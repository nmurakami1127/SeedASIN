"""レーン設定 extra_origin_imports（既存の出典の取得候補だけに反対側市場のimportを加える）の単体テスト。DB・ブラウザ・実データ不要
（合成データと疑似のセッション/レポート台帳のみ）。docs/DEVIATIONS.md #100。"""
from __future__ import annotations

import argparse
import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import acquisition.plan as plan_mod
from acquisition.eligibility import Config, RunFact, SourceFacts
from acquisition.plan import PlanError, build_plan
from workflow import cli as wcli
from workflow.config import ConfigError, config_from_dict
from workflow.engine import Engine
from workflow.lane_imports import has_lane_path, lane_origin_imports, parse_extra_arg, validate_lane_imports, with_extra_override
from workflow.source_select import choose, engine_selection, extra_effect
from workflow.store import State, Task

AS_OF = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
# 固定の旧実装（extra導入前）で同じ設定から計算した sha256。extraが空のとき、設定の同一性判定が変わらないことを固定する。
OLD_SHA = "2b5d24c405c0051330cf0a9ceb191e4c872667285dfc7a51419ad8c01fcb2224"
BASE = {"lanes": {"US": {"origin_import": 1}, "JP": {"origin_import": 2}}}


# ---------------------------------------------------------------- 疑似のDB・レポート台帳

def path(sid, imp, direction, units, asin="B0ORIGIN01"):
    return SimpleNamespace(source_id=sid, direction=direction, origin_marketplace="US" if direction == "EXPORT" else "JP", origin_import_id=imp,
                           origin_asin=f"{asin}{imp}", origin_units=units, origin_sessions=units * 2, origin_buybox_max_pct=1.5)


def facts(sid, market="US", runs=()):
    return SourceFacts(sid, market, "SELLER", f"seller{sid}", "", f"h{sid}", f"Seller{sid}", "", "ACTIVE", None, None, None, None, runs=list(runs))


def fetched_recently(run_id=900):
    return RunFact(run_id, "COMPLETED", "SUCCESS_NONEMPTY", AS_OF - timedelta(days=1), None, cycle_id=1, cycle_seq=1, cycle_state="COMPLETE")


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def begin(self):
        return nullcontext()

    def execute(self, _stmt, params=None):
        ids = set((params or {}).get("ids") or [])
        found = [r for r in self.rows if r.origin_import_id in ids]
        return SimpleNamespace(all=lambda: found, scalar_one=lambda: AS_OF)


class FakeFactory:
    def __init__(self, rows_by_market):
        self.rows_by_market = rows_by_market

    @contextmanager
    def get_session(self, market):
        yield FakeConn(self.rows_by_market.get(market, []))


def report(import_id, market, status="ACTIVE", error_rows=0):
    return SimpleNamespace(import_id=import_id, marketplace=market, status=status, error_rows=error_rows,
                           business_direction="EXPORT" if market == "US" else "IMPORT", period_start="2026-10-01", period_end="2026-10-31")


class FakeReports:
    def __init__(self, *infos):
        self.infos = {i.import_id: i for i in infos}

    def resolve_market_for_import(self, iid):
        i = self.infos.get(iid)
        return i.marketplace if i else None

    def get_report(self, iid, mp):
        return self.infos.get(iid)


REPORTS = FakeReports(report(1, "US"), report(2, "JP"), report(3, "US"), report(4, "JP", status="SUPERSEDED"), report(5, "JP", error_rows=2), report(6, "JP"))

# US市場の出典: 1,2=レポート1(EXPORT)だけ / 3,4=レポート2(IMPORT)だけ（NO_ORIGINの原因）/ 5=両方 / 6=レポート2だけだが1日前に取得済み / 7=経路なし
US_ROWS = [path(1, 1, "EXPORT", 10), path(2, 1, "EXPORT", 5), path(3, 2, "IMPORT", 8), path(4, 2, "IMPORT", 3),
           path(5, 1, "EXPORT", 7), path(5, 2, "IMPORT", 9), path(6, 2, "IMPORT", 6)]
US_FACTS = [facts(1), facts(2), facts(3), facts(4), facts(5), facts(6, runs=[fetched_recently()]), facts(7)]
JP_ROWS = [path(21, 2, "IMPORT", 10), path(22, 1, "EXPORT", 4)]
JP_FACTS = [facts(21, "JP"), facts(22, "JP")]


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(plan_mod, "load_configs", lambda conn, market: {"SELLER": Config()})
    monkeypatch.setattr(plan_mod, "load_facts", lambda conn, market: {"US": US_FACTS, "JP": JP_FACTS}[market])
    return FakeFactory({"US": US_ROWS, "JP": JP_ROWS})


def plan_for(world, imports, market="US"):
    return build_plan(world, REPORTS, market, imports, as_of=AS_OF)


def ids_in(plan, section):
    return [e.facts.source_id for e in plan.sections.get(section, [])]


def cfg_with(**lanes):
    return config_from_dict({"lanes": lanes, "completion": {"reserve_unevaluated": 0}})


def progress_stub():
    return SimpleNamespace(slow_map=lambda m: {}, evaluated=lambda m: set())


# ---------------------------------------------------------------- (a) 既定は空で、挙動・ハッシュが変わらない

def test_default_is_empty_and_config_identity_is_unchanged():
    cfg = config_from_dict(BASE)
    assert cfg.lanes["US"].extra_origin_imports == [] and lane_origin_imports(cfg.lanes["US"]) == [1]
    assert cfg.sha256() == OLD_SHA                                     # 空のときは従来と同じハッシュ
    assert "extra_origin_imports" not in cfg.to_dict()["lanes"]["US"]  # スナップショット（State.config）も従来と同じ形
    assert "extra_origin_imports" not in cfg.for_lane("US").to_dict()["lanes"]["US"]
    assert config_from_dict(cfg.to_dict()).sha256() == OLD_SHA          # 保存済みスナップショットからの復元（watch）


def test_extra_is_part_of_the_snapshot_and_hash_only_when_set():
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1, "extra_origin_imports": [2]}, "JP": {"origin_import": 2}}})
    assert cfg.sha256() != OLD_SHA
    assert cfg.to_dict()["lanes"]["US"]["extra_origin_imports"] == [2] and "extra_origin_imports" not in cfg.to_dict()["lanes"]["JP"]
    assert cfg.for_lane("US").lanes["US"].extra_origin_imports == [2]
    again = config_from_dict(cfg.to_dict())
    assert again.sha256() == cfg.sha256() and lane_origin_imports(again.lanes["US"]) == [1, 2]


def test_default_selection_ignores_sources_that_only_have_the_other_reports_paths(world):
    plan = plan_for(world, [1])
    assert sorted(ids_in(plan, "NO_ORIGIN")) == [3, 4, 7] and 6 not in ids_in(plan, "NO_ORIGIN")
    sel = engine_selection(plan, progress_stub(), "US", cfg_with(US={"origin_import": 1}))
    assert [e.facts.source_id for e in sel.picks] == [1, 5, 2] and sel.selectable == 3


# ---------------------------------------------------------------- (b) US=[2] で NO_ORIGIN だった出典が候補になる

def test_extra_turns_no_origin_sources_into_candidates(world):
    plan = plan_for(world, [1, 2])
    assert ids_in(plan, "NO_ORIGIN") == [7]
    assert sorted(ids_in(plan, "IMPORT")) == [3, 4, 5] and sorted(ids_in(plan, "EXPORT")) == [1, 2, 5]
    cfg = cfg_with(US={"origin_import": 1, "extra_origin_imports": [2]})
    sel = engine_selection(plan, progress_stub(), "US", cfg, primary_import_ids=[1])
    got = [e.facts.source_id for e in sel.picks]
    assert sorted(got) == [1, 2, 3, 4, 5] and len(got) == len(set(got))
    assert 6 not in got and 7 not in got               # 取得間隔内（6）・経路なし（7）は候補にならない
    assert sel.selectable == 5


def test_origin_import_candidates_come_before_extra_candidates(world):
    plan = plan_for(world, [1, 2])
    cfg = cfg_with(US={"origin_import": 1, "extra_origin_imports": [2]})
    us = [e.facts.source_id for e in engine_selection(plan, progress_stub(), "US", cfg, primary_import_ids=[1]).picks]
    assert us == [1, 5, 2, 3, 4]                       # origin_import(EXPORT)由来 → extra(IMPORT)由来
    jp_like = [e.facts.source_id for e in choose(plan, primary_import_ids=[2]).picks]
    assert jp_like == [5, 3, 4, 1, 2]                  # origin_importがIMPORT側のレーンでは、IMPORT由来が先
    assert [e.facts.source_id for e in choose(plan).picks] == [1, 5, 2, 3, 4]  # primary指定なし=従来の方向順


def test_continue_and_other_plans_keep_their_priority_over_group_order(world, monkeypatch):
    cont = RunFact(901, "COMPLETED", "SUCCESS_TRUNCATED", AS_OF - timedelta(days=2), None, cycle_id=2, cycle_seq=1, cycle_state="OPEN",
                   next_url="https://x/?page=3", next_page_no=3, last_page_url="https://x/?page=2", last_page_signature="s", last_page_asins=("B0AAAAAAA1",))
    monkeypatch.setattr(plan_mod, "load_facts", lambda conn, market: [facts(1), facts(3, runs=[cont]), facts(4)])
    plan = plan_for(world, [1, 2])
    assert [(e.facts.source_id, e.group) for e in plan.sections["IMPORT"]] == [(3, "CONTINUE"), (4, "NEVER")]  # セクション内はCONTINUEが先（従来どおり）
    assert plan.sections["IMPORT"][0].decision.plan == "CONTINUE"


# ---------------------------------------------------------------- (c) 二重取得の防止

def test_a_source_in_both_directions_is_one_candidate_and_one_fetch(world):
    plan = plan_for(world, [1, 2])
    assert 5 in ids_in(plan, "EXPORT") and 5 in ids_in(plan, "IMPORT")        # 両方向に載る
    sel = choose(plan, primary_import_ids=[1])
    assert [e.facts.source_id for e in sel.picks].count(5) == 1                  # 取得は出典単位で1回
    entries = {e.facts.source_id: e for es in plan.sections.values() for e in es}  # _acquire_sourcesの索引: 出典で一意
    assert len(entries) == len({e.facts.source_id for es in plan.sections.values() for e in es})
    eff = extra_effect(plan_for(world, [1]), plan, {}, set(), primary_import_ids=[1])
    assert eff["duplicated"] == 1


def test_fetched_source_is_not_a_candidate_in_the_next_plan_within_the_interval(world, monkeypatch):
    first = plan_for(world, [1, 2])
    assert 3 in [e.facts.source_id for e in choose(first, primary_import_ids=[1]).picks]
    monkeypatch.setattr(plan_mod, "load_facts", lambda conn, market: [facts(s.source_id, runs=[fetched_recently()] if s.source_id in (3, 5) else s.runs)
                                                                     for s in US_FACTS])
    second = plan_for(world, [1, 2])                                             # 取得の後（時間をずらした2回目）
    got = [e.facts.source_id for e in choose(second, primary_import_ids=[1]).picks]
    assert 3 not in got and 5 not in got and sorted(got) == [1, 2, 4]
    assert {f.source_id for f, d in second.blocked} >= {3, 5, 6}
    assert all(any(b.code == "TOO_RECENT" for b in d.blockers) for f, d in second.blocked if f.source_id in (3, 5, 6))


# ---------------------------------------------------------------- (d) 方向の整合

def test_direction_must_have_a_path_from_one_of_the_lane_imports():
    imp = {"direction": "IMPORT", "origin_import_id": 2}
    exp = {"direction": "EXPORT", "origin_import_id": 1}
    assert has_lane_path([imp], "IMPORT", [1, 2]) and not has_lane_path([imp], "IMPORT", [1])      # extraなしのレーンは従来どおり見送り
    assert not has_lane_path([imp], "EXPORT", [1, 2])                                              # IMPORTの出典はEXPORTとして投入されない
    assert not has_lane_path([{"direction": "EXPORT", "origin_import_id": 9}], "EXPORT", [1, 2])
    assert has_lane_path([imp, exp], "EXPORT", [1, 2]) and has_lane_path([imp, exp], "IMPORT", [1, 2])


def _engine_for_expand(monkeypatch, cfg, items, reports=REPORTS):
    import workflow.engine as E

    eng = Engine.__new__(Engine)
    eng.cfg, eng.reports, eng.factory, eng.ledger = cfg, reports, object(), object()
    eng.now = lambda: AS_OF
    eng.store = SimpleNamespace(save=lambda st: None)
    eng.st = State("wf-x", "now", {}, "h")
    written = []
    monkeypatch.setattr(E, "build_items", lambda *a, **k: (items, [b"x"] * len(items)))
    monkeypatch.setattr(E, "write_plan", lambda its, files, ledger: written.extend(its))
    return eng, written


def _item(key, direction, imports):
    return SimpleNamespace(key=key, collector_run_id=100, direction=direction, memo=f"{direction}_memo", asin_count=3, business_market="US" if direction == "EXPORT" else "JP",
                           source_id=11, input_is_partial=False, repeat_of_earlier_cycle=False, same_cycle_excluded=0, also_from=[],
                           paths=[{"direction": direction, "origin_import_id": i} for i in imports])


def _expand(eng, acq_kind):
    acq = Task(f"{acq_kind}:US", acq_kind, "US", status="DONE", result={"market": "US", "sources": {"11": {"mode": "FETCHED", "run_id": 100}}})
    t = Task("expand:US", "expand", "US", deps=[acq.id])
    eng.st.tasks.update({acq.id: acq, t.id: t})
    eng._run_expand(t)
    return t


def test_expand_keeps_only_directions_with_a_path_from_the_lane_imports(monkeypatch):
    cfg = cfg_with(US={"origin_import": 1, "extra_origin_imports": [2]})
    items = [_item("k-imp", "IMPORT", [2]), _item("k-exp-other", "EXPORT", [9])]
    eng, written = _engine_for_expand(monkeypatch, cfg, items)
    t = _expand(eng, "acquire")
    assert t.result["keys"] == ["k-imp"] and [i.key for i in written] == ["k-imp"]
    sub = eng.st.tasks["submit:k-imp"].result
    assert (sub["direction"], sub["business_market"], sub["memo"]) == ("IMPORT", "JP", "IMPORT_memo")  # IMPORTの出典はIMPORTのまま
    assert [s["direction"] for s in t.result["skipped"]] == ["EXPORT"] and "経路が無い方向" in t.result["skipped"][0]["reason"]


def test_without_extra_an_import_derived_item_is_still_skipped(monkeypatch):
    eng, written = _engine_for_expand(monkeypatch, cfg_with(US={"origin_import": 1}), [_item("k-imp", "IMPORT", [2])])
    t = _expand(eng, "acquire")
    assert t.result["keys"] == [] and written == [] and len(t.result["skipped"]) == 1


def test_extra_is_not_used_for_the_cross_acquire_results(monkeypatch):
    cfg = cfg_with(US={"origin_import": 1, "extra_origin_imports": [2]})
    eng, written = _engine_for_expand(monkeypatch, cfg, [_item("k-imp", "IMPORT", [2]), _item("k-exp", "EXPORT", [1])])
    t = _expand(eng, "acquire_x")                      # crossで発見した反対側の出典: 従来どおり origin_import だけ
    assert t.result["keys"] == ["k-exp"]


# ---------------------------------------------------------------- (e) 不正なextraの拒否と他市場への非波及

@pytest.mark.parametrize("bad", [[0], [-1], [True], ["2"], [2, 2], [1], "2", 2])
def test_config_rejects_invalid_extra(bad):
    with pytest.raises(ConfigError):
        config_from_dict({"lanes": {"US": {"origin_import": 1, "extra_origin_imports": bad}}})


@pytest.mark.parametrize("extra,why", [([3], "市場"), ([99], "存在しません"), ([4], "ACTIVE"), ([5], "行エラー"), ([2, 5], "行エラー")])
def test_validate_lane_imports_rejects_same_market_unknown_inactive(extra, why):
    ln = SimpleNamespace(origin_import=1, extra_origin_imports=extra)
    with pytest.raises(PlanError, match=why):
        validate_lane_imports(REPORTS, "US", ln)


def test_validate_lane_imports_accepts_the_opposite_market_and_returns_origin_first():
    infos = validate_lane_imports(REPORTS, "US", SimpleNamespace(origin_import=1, extra_origin_imports=[2]))
    assert [i.import_id for i in infos] == [1, 2]
    assert [i.import_id for i in validate_lane_imports(REPORTS, "US", SimpleNamespace(origin_import=1, extra_origin_imports=[]))] == [1]
    with pytest.raises(PlanError, match="市場"):  # JPレーンのextraにJP市場のレポートは不可
        validate_lane_imports(REPORTS, "JP", SimpleNamespace(origin_import=2, extra_origin_imports=[6]))


def test_a_bad_extra_stops_only_its_own_lane():
    cfg = cfg_with(US={"origin_import": 1, "extra_origin_imports": [3]}, JP={"origin_import": 2, "extra_origin_imports": [1]})
    eng = Engine.__new__(Engine)
    eng.cfg, eng.reports = cfg, REPORTS
    with pytest.raises(PlanError):
        eng._lane_info("US")
    ln, info = eng._lane_info("JP")                    # もう一方の市場のレーンは影響を受けない
    assert (info.import_id, ln.extra_origin_imports) == (2, [1])
    assert lane_origin_imports(ln) == [2, 1]


def test_the_plan_itself_refuses_two_reports_of_one_market(world):
    with pytest.raises(PlanError):
        plan_for(world, [1, 3])                        # 多重の防御: 同じ市場のimportを混ぜない


# ---------------------------------------------------------------- (f) plan-compare の上書き（読み取り専用）

def test_override_parser_and_in_memory_override():
    assert parse_extra_arg("US=2,JP=1") == {"US": [2], "JP": [1]} and parse_extra_arg("US=") == {"US": []}
    for bad in ("US", "US=x"):
        with pytest.raises(ConfigError):
            parse_extra_arg(bad)
    cfg = config_from_dict(BASE)
    over = with_extra_override(cfg, {"US": [2]})
    assert over.lanes["US"].extra_origin_imports == [2] and cfg.lanes["US"].extra_origin_imports == []   # 元の設定は変えない
    with pytest.raises(ConfigError):
        with_extra_override(cfg, {"US": [1]})          # origin_import自身
    with pytest.raises(ConfigError):
        with_extra_override(config_from_dict({"lanes": {"US": {"origin_import": 1}}}), {"JP": [1]})


def _compare(tmp_path, monkeypatch, world, capsys, override, market="ALL", config=None):
    import origin_selection.report_import as RI

    p = tmp_path / "workflow.json"
    p.write_text(json.dumps(config or {**BASE, "state_dir": str(tmp_path / "wf")}), encoding="utf-8")
    before = p.read_bytes()
    monkeypatch.setattr(RI, "ReportImportService", lambda *a, **k: REPORTS)
    args = argparse.Namespace(config=str(p), market=market, simulate_shortage=None, extra_origin_imports=override)
    rc = wcli._cmd_plan_compare(args, factory=world)
    assert p.read_bytes() == before                      # 設定ファイルは書き換えない
    return rc, capsys.readouterr().out


def test_plan_compare_override_reports_before_and_after(tmp_path, monkeypatch, world, capsys):
    rc, out = _compare(tmp_path, monkeypatch, world, capsys, "US=2")
    assert rc == 0
    assert "US extra_origin_imports の効果" in out and "JP extra_origin_imports の効果" not in out     # 上書きした市場だけ
    assert "取得候補の出典数: 導入前 3件 → 導入後 5件" in out
    assert "新たに加わる出典 2件" in out and "#3 #4" in out
    assert "NO_ORIGIN（起点の経路が無く候補に入らない出典）: 導入前 3件 → 導入後 1件" in out
    assert "EXPORT・IMPORTの両方に現れる出典: 1件" in out


def test_plan_compare_without_override_is_unchanged(tmp_path, monkeypatch, world, capsys):
    rc, out = _compare(tmp_path, monkeypatch, world, capsys, None)
    assert rc == 0 and "extra_origin_imports の効果" not in out and "取得対象の比較" in out


def test_plan_compare_bad_override_stops_only_that_market(tmp_path, monkeypatch, world, capsys):
    rc, out = _compare(tmp_path, monkeypatch, world, capsys, "US=3,JP=1")   # US=3 はUS市場のレポート（不可）、JP=1 は正しい
    assert rc == 0
    assert "[US] 計画を作れません" in out and "市場" in out
    assert "JP extra_origin_imports の効果" in out                           # JPの比較は続く


def _cross_engine(monkeypatch, cfg, has_path):
    import workflow.engine as E

    eng = Engine.__new__(Engine)
    eng.cfg, eng.factory, eng.now, eng.reports = cfg, object(), (lambda: AS_OF), REPORTS
    eng.store = SimpleNamespace(save=lambda st: None)
    eng.st = State("wf-x", "now", {}, "h")
    monkeypatch.setattr(E, "require_own_accounts", lambda *a, **k: None)
    calls = []
    eng._has_origin_path = lambda market, sid, imp: calls.append((market, sid, imp)) or has_path

    def boom(*a, **k):
        raise AssertionError("extra由来の出典でcrossの選択が呼ばれた")

    eng.svc = SimpleNamespace(select_cross_asins=boom)
    acq = Task("acquire:US", "acquire", "US", status="DONE", result={"market": "US", "sources": {"11": {"mode": "FETCHED", "run_id": 5}}})
    t = Task("cross:US", "cross", "US", deps=[acq.id])
    eng.st.tasks.update({acq.id: acq, t.id: t})
    return eng, t, calls


def test_cross_skips_sources_without_an_origin_import_path_when_extra_is_set(monkeypatch):
    """extra由来の出典（origin_importの起点経路が無い）はcrossの対象外。selectが拒否して反対側展開が保留のままになるのを避ける。"""
    eng, t, calls = _cross_engine(monkeypatch, cfg_with(US={"origin_import": 1, "extra_origin_imports": [2]}), has_path=False)
    eng._run_cross(t)
    assert calls == [("US", 11, 1)]                       # 判定は origin_import だけで行う
    assert t.status == "DONE" and t.result["via"]["11"]["ok"] is True and t.result["via"]["11"]["investigated"] == 0


def test_plan_compare_unknown_lane_in_override_is_a_config_error(tmp_path, monkeypatch, world, capsys):
    with pytest.raises(ConfigError):
        _compare(tmp_path, monkeypatch, world, capsys, "JP=1", config={"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path / "wf")})
