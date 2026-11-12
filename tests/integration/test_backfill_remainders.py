"""過去の周期に取り残された保存済みASINの回収 `backfill-remainders`（docs/DEVIATIONS.md #105）と、保留(ON_HOLD)出典の保存済みASINの投入計画（依頼B）の結合テスト。
専用test-db・疑似ページ。外部接続なし。DBは読み取りだけ（出典の状態を変えない）ことも確認する。

依頼A: dry-runで何も書かない／--applyで期待どおり計画／2回目は0件／PLAN_WITHDRAWN・TRACKING_ABANDONED・結果不明は復活しない／IMPORT・EXPORT分離とレーン経路なし／
5,001・12,300・20,000件の分割／他の周期・出典の重複除外と--include-overlap／ON_HOLD出典の回収と保留状態の不変／恒等式／レポート出力。
依頼B: ON_HOLD出典の保存済みASINが通常の計画（watch・engine）に入る／取得側の判定は変わらない／EXCLUDED・PERMANENTLY_EXCLUDEDは入らない。
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from acquisition.registry import SourceRegistry
from asin_collector.source_runner import load_source, source_url
from tests.integration.acq_helpers import make_active_report, make_runs, unique_key
from tests.integration.test_continuation import Site, fast_cfg  # noqa: F401
from tests.integration.test_flush_unfilled import go
from tests.integration.test_workflow import own  # noqa: F401
from tool_submission import backfill
from tool_submission.cli import main as cli_main
from tool_submission.ledger import SubmissionLedger
from workflow.buffer_recovery import stage_stalled_inputs
from workflow.config import config_from_dict
from workflow.store import WorkflowStore

pytestmark = pytest.mark.integration

LATER = datetime.now(timezone.utc) + timedelta(hours=2)


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


def pool(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:07d}" for i in range(n)]


def paged(asins: list[str], per: int = 2500) -> list[list[str]]:
    return [asins[i:i + per] for i in range(0, len(asins), per)]


class W:
    """US・JPのレポート（起点ASINを複数）と、出典・取得・設定をまとめた架空データ。出典は同じレポートの別の起点ASINに紐づける。"""

    def __init__(self, factory, registry, runs, tmp_path, n_origins: int = 12) -> None:
        self.f, self.reg, self.runs, self.tmp = factory, registry, runs, tmp_path
        self.us = make_active_report(factory, tmp_path, "US", [(f"B0BFU{i:05d}", 80, 4.0, 7) for i in range(n_origins)], name="bfu.csv")
        self.jp = make_active_report(factory, tmp_path, "JP", [(f"B0BFJ{i:05d}", 80, 4.0, 3) for i in range(n_origins)], name="bfj.csv")
        self.n = itertools.count()
        self.ledger_path = tmp_path / "sub" / "ledger.jsonl"

    def cfg(self, *, extra: bool = True, **kw):
        lane = {"origin_import": self.us, **({"extra_origin_imports": [self.jp]} if extra else {})}
        return config_from_dict({"lanes": {"US": lane}, "submission_dir": str(self.tmp / "sub"), "state_dir": str(self.tmp / "wf"),
                                 "batch_target_asins": 5000, "skip_failures": True, **kw})

    def ledger(self) -> SubmissionLedger:
        return SubmissionLedger(self.ledger_path)

    def source(self, asins: list[str], *, dirs=("EXPORT", "IMPORT"), hold: str | None = None, per: int = 2500, fetch_pages: int = 99) -> int:
        i = next(self.n)
        key = unique_key("ABF")
        rp = None
        if "EXPORT" in dirs:
            rp = self.reg.register_path("US", "SELLER", key, origin_import_id=self.us, origin_asin=f"B0BFU{i:05d}")
        if "IMPORT" in dirs:
            rp = self.reg.register_path("US", "SELLER", key, origin_import_id=self.jp, origin_asin=f"B0BFJ{i:05d}")
        sid = rp.source.source_id
        site = Site(paged(asins, per), source_url("US", load_source(self.f, "US", sid)))
        go(self.runs, self.f, site, sid, fetch_pages)
        if hold:
            self.reg.set_status("US", sid, hold, "テスト")
        return sid

    def status(self, sid: int):
        with self.f.get_session("US") as conn, conn.begin():
            return tuple(conn.execute(text("SELECT status, hold_reason, hold_detail FROM acquisition_source WHERE source_id=:s"), {"s": sid}).one())


def asins_of(items) -> list[str]:
    return [a for e in items for a in e["content"].decode().split()]


def run(w: W, cfg, sids, **kw):
    kw.setdefault("now", LATER)
    return backfill.compute(w.f, cfg, w.ledger(), sources=set(sids), **kw)


def by_dir(res, d):
    return [e for e in res["items"] if e["item"].direction == d]


def row_of(res, sid, d):
    return next(r for r in res["rows"] if r["source_id"] == sid and r["direction"] == d)


# ---------------------------------------------------------------- 依頼A: dry-run・apply・冪等・保留状態の不変

def test_dry_run_writes_nothing_then_apply_plans_every_asin_and_the_second_apply_adds_nothing(app_role_factory, registry, runs, fast_cfg, tmp_path, superuser_url):
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool("B5A", 12300)
    sid = w.source(asins, hold="ON_HOLD")  # 取得・保存済みだが、保留中の出典
    held_before = w.status(sid)
    assert held_before[0] == "ON_HOLD"
    cfg = w.cfg()
    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    res = run(w, cfg, [sid])
    assert res["identity_ok"] and not res["errors"]
    for d in ("EXPORT", "IMPORT"):  # 方向ごとに別の入力。端数も含めて全件・5,000件以下
        its = by_dir(res, d)
        assert [e["item"].asin_count for e in its] == [5000, 5000, 2300] and asins_of(its) == asins
        assert row_of(res, sid, d)["reasons"]["register"] == 12300 and row_of(res, sid, d)["total"] == 12300
        assert len({e["item"].key for e in its}) == 3 and all(e["item"].memo.startswith("輸出_" if d == "EXPORT" else "輸入_") for e in its)
    assert not w.ledger_path.exists() and not (tmp_path / "sub").exists()  # computeは何も書かない（台帳・入力ファイル）
    # CLI dry-run: 台帳・入力ファイル・DBに書かず、レポートだけ書く
    rdir = tmp_path / "rep1"
    assert cli_main(["backfill-remainders", "--config", str(cfgfile), "--source", str(sid), "--grace-minutes", "0", "--report-dir", str(rdir)],
                    factory=app_role_factory) == 0
    assert not w.ledger_path.exists() and not (tmp_path / "sub").exists()
    assert {p.name for p in rdir.iterdir()} == {"before.json", "plan.json", "summary.txt"}
    plan = json.loads((rdir / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["inputs"]) == 6 and sum(i["asin_count"] for i in plan["inputs"]) == 24600 and plan["applied"] is False
    assert w.status(sid) == held_before  # 保留状態は不変
    # --apply: 確認の拒否では何も書かない
    rdir2 = tmp_path / "rep2"
    args = ["backfill-remainders", "--config", str(cfgfile), "--source", str(sid), "--grace-minutes", "0", "--report-dir", str(rdir2), "--apply"]
    assert cli_main(args, factory=app_role_factory, confirm=lambda q: False) == 2
    assert not w.ledger_path.exists()
    assert cli_main(args, factory=app_role_factory, confirm=lambda q: True) == 0
    st = w.ledger().all_states()
    assert len(st) == 6 and {s.state for s in st.values()} == {"PLANNED"}
    assert sorted((s.manifest["direction"], s.manifest["asin_count"]) for s in st.values()) == [("EXPORT", 2300), ("EXPORT", 5000), ("EXPORT", 5000),
                                                                                                    ("IMPORT", 2300), ("IMPORT", 5000), ("IMPORT", 5000)]
    for s in st.values():  # 各バッチに一意の投入鍵・CSV・manifest
        m = s.manifest
        assert Path(m["input_file"]).is_file() and (Path(m["input_file"]).parent / f"manifest_{s.key}.json").is_file()
        assert m["fetch_market"] == "US" and m["source_id"] == sid and m["cycle_key"].startswith("c")
    assert w.status(sid) == held_before  # 保留状態は不変（出典のstatus・hold_reason・hold_detail）
    assert {p.name for p in rdir2.iterdir()} == {"before.json", "plan.json", "after.json", "summary.txt"}
    after = json.loads((rdir2 / "after.json").read_text(encoding="utf-8"))
    assert after["ledger_diff"]["keys"] == 6 and after["ledger_diff"]["by_direction_state"]["EXPORT/PLANNED"] == {"keys": 3, "asins": 12300}
    assert after["ledger_before"]["keys"] == 0 and after["ledger_after"]["keys"] == 6
    n_events = len(w.ledger().events())
    # 2回目は0件（同じASINを重複計画しない）
    res2 = run(w, cfg, [sid])
    assert res2["items"] == [] and row_of(res2, sid, "EXPORT")["reasons"]["planned_same_cycle"] == 12300 and res2["identity_ok"]
    assert cli_main(args, factory=app_role_factory, confirm=lambda q: True) == 0
    assert len(w.ledger().events()) == n_events and len(w.ledger().all_states()) == 6
    # DBには一切書いていない（保留中の出典のまま・取得履歴も変えていない）
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM collector_run WHERE source_id=:s"), {"s": sid}).scalar_one() == 1


@pytest.mark.parametrize("n,expected", [(5001, [5000, 1]), (12300, [5000, 5000, 2300]), (20000, [5000] * 4)])
def test_split_sizes_cover_every_asin_without_duplicates(app_role_factory, registry, runs, fast_cfg, tmp_path, n, expected):
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool(f"B5{n % 7}", n)
    sid = w.source(asins, dirs=("EXPORT",), hold="ON_HOLD")
    res = run(w, w.cfg(), [sid])
    its = res["items"]
    assert [e["item"].asin_count for e in its] == expected and asins_of(its) == asins and len(set(asins_of(its))) == n
    assert len({e["item"].key for e in its}) == len({e["item"].input_sha256 for e in its}) == len(its)
    assert all(e["item"].asin_count <= 5000 and e["item"].direction == "EXPORT" for e in its)
    assert len({e["item"].split_parent["id"] for e in its}) == 1
    r = row_of(res, sid, "EXPORT")
    assert r["identity_ok"] and r["reasons"]["register"] == n and r["chunks"] == expected


# ---------------------------------------------------------------- 取り下げ・追跡取り下げ・結果不明は復活しない

def test_withdrawn_abandoned_and_unknown_asins_are_never_revived(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool("B5W", 20000)
    sid = w.source(asins, dirs=("EXPORT",), hold="ON_HOLD")
    cfg = w.cfg()
    first = run(w, cfg, [sid])
    ledger = w.ledger()
    backfill.apply(first, ledger, Path(cfg.submission_dir))
    k1, k2, k3, k4 = (e["item"].key for e in first["items"])
    ledger.append(k1, "PLAN_WITHDRAWN", reason="小口の再編成")
    ledger.append(k2, "SUBMIT_STARTED", before_run_ids=[1], options={})
    ledger.append(k2, "OUTCOME_UNKNOWN", reason="t")
    ledger.append(k3, "SUBMIT_STARTED", before_run_ids=[1], options={})
    ledger.append(k3, "OUTCOME_UNKNOWN", reason="t")
    ledger.append(k3, "TRACKING_ABANDONED", reason="人の指示")
    st = ledger.all_states()
    assert (st[k1].state, st[k2].state, st[k3].state, st[k4].state) == ("WITHDRAWN", "UNKNOWN", "ABANDONED", "PLANNED")
    n_events = len(ledger.events())
    res = run(w, cfg, [sid])
    r = row_of(res, sid, "EXPORT")["reasons"]
    assert res["items"] == [] and res["identity_ok"]  # 何も復活・再計画しない
    assert (r["withdrawn"], r["unknown_outcome"], r["abandoned"], r["planned_same_cycle"], r["register"]) == (5000, 5000, 5000, 5000, 0)
    assert cli_main(["backfill-remainders", "--config", _write_cfg(tmp_path, cfg), "--source", str(sid), "--grace-minutes", "0",
                     "--report-dir", str(tmp_path / "r"), "--apply"], factory=app_role_factory, confirm=lambda q: True) == 0
    assert len(ledger.events()) == n_events  # 台帳に何も追記しない
    st = ledger.all_states()
    assert (st[k1].state, st[k2].state, st[k3].state) == ("WITHDRAWN", "UNKNOWN", "ABANDONED")  # 取り下げ・結果不明・追跡取り下げのまま


def _write_cfg(tmp_path, cfg) -> str:
    p = tmp_path / "cfg_used.json"
    p.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- IMPORT/EXPORTの分離とレーンの経路なし

def test_directions_are_separated_and_a_direction_without_a_lane_path_is_not_planned(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool("B5D", 6000)
    sid = w.source(asins, hold="ON_HOLD")
    # レーンにextra(JPレポート)が無い: IMPORTの経路はレーンに無い → EXPORTだけ計画。IMPORTは「レーン経路なし」で件数だけ集計
    res = run(w, w.cfg(extra=False), [sid])
    assert {e["item"].direction for e in res["items"]} == {"EXPORT"} and asins_of(res["items"]) == asins
    ri = row_of(res, sid, "IMPORT")["reasons"]
    assert (ri["no_lane_path"], ri["register"]) == (6000, 0) and row_of(res, sid, "IMPORT")["identity_ok"]
    assert res["totals"]["by_direction"]["IMPORT"]["reasons"]["no_lane_path"] == 6000
    # --direction IMPORT だけ: レーン経路が無いので計画ゼロ（EXPORTは対象にしない）
    only = run(w, w.cfg(extra=False), [sid], directions={"IMPORT"})
    assert only["items"] == [] and {r["direction"] for r in only["rows"]} == {"IMPORT"}
    # extra があれば両方向を別々に（混在しない）
    both = run(w, w.cfg(), [sid])
    assert sorted(e["item"].direction for e in both["items"]) == ["EXPORT", "EXPORT", "IMPORT", "IMPORT"]
    assert all(e["item"].business_market == ("US" if e["item"].direction == "EXPORT" else "JP") for e in both["items"])
    ex_only = run(w, w.cfg(), [sid], directions={"EXPORT"})
    assert {e["item"].direction for e in ex_only["items"]} == {"EXPORT"}


# ---------------------------------------------------------------- 他の周期・出典での重複

def test_overlap_with_other_sources_is_excluded_by_default_and_included_with_the_flag(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    a_asins = pool("B5X", 1000)
    b_asins = a_asins[:300] + pool("B5Y", 700)  # Bは、Aの300件と重なる
    sa = w.source(a_asins, dirs=("EXPORT",), hold="ON_HOLD")
    sb = w.source(b_asins, dirs=("EXPORT",), hold="ON_HOLD")
    cfg = w.cfg()
    res = run(w, cfg, [sa, sb])
    ra, rb = row_of(res, sa, "EXPORT")["reasons"], row_of(res, sb, "EXPORT")["reasons"]
    assert (ra["register"], rb["register"], rb["overlap_other_source"]) == (1000, 700, 300)  # 古い取得から順に確定。重なりは実際の投入対象から除く
    got = asins_of(res["items"])
    assert len(got) == len(set(got)) == 1700  # 同じ方向で同じASINを二重に計画しない
    assert res["identity_ok"] and res["totals"]["reasons"]["overlap_other_source"] == 300
    inc = run(w, cfg, [sa, sb], include_overlap=True)
    assert row_of(inc, sb, "EXPORT")["reasons"]["register"] == 1000 and row_of(inc, sb, "EXPORT")["reasons"]["overlap_other_source"] == 0
    assert len(asins_of(inc["items"])) == 2000 and inc["identity_ok"]
    # すでに台帳で計画済み（Aを適用済み）の場合も同じ: Bだけを見ても300件を除く。再実行しても増えない
    backfill.apply(run(w, cfg, [sa]), w.ledger(), Path(cfg.submission_dir))
    only_b = run(w, cfg, [sb])
    assert row_of(only_b, sb, "EXPORT")["reasons"]["overlap_other_source"] == 300 and asins_of(only_b["items"]) == b_asins[300:]
    # 別の方向は別に判定する（EXPORTで計画済みでも、IMPORTのASINは除外されない）
    sc = w.source(a_asins[:100], dirs=("IMPORT",), hold="ON_HOLD")
    assert row_of(run(w, cfg, [sc]), sc, "IMPORT")["reasons"]["register"] == 100


# ---------------------------------------------------------------- 出典の状態・猶予・絞り込み

def test_source_status_grace_and_filters(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    held = w.source(pool("B5H", 30), dirs=("EXPORT",), hold="ON_HOLD")
    excl = w.source(pool("B5E", 20), dirs=("EXPORT",), hold="EXCLUDED")
    act = w.source(pool("B5C", 10), dirs=("EXPORT",))
    cfg = w.cfg()
    res = run(w, cfg, [held, excl, act])
    assert row_of(res, held, "EXPORT")["reasons"]["register"] == 30 and row_of(res, act, "EXPORT")["reasons"]["register"] == 10
    assert (row_of(res, excl, "EXPORT")["reasons"]["source_excluded"], row_of(res, excl, "EXPORT")["reasons"]["register"]) == (20, 0)  # 除外は対象外（理由を集計）
    assert res["totals"]["by_source_status"] == {"ON_HOLD/MANUAL": 30, "ACTIVE/-": 10}
    # 猶予内（now=現在）: 猶予待ちとして集計し、計画しない。--ignore-graceで含める
    waiting = backfill.compute(w.f, cfg, w.ledger(), sources={held}, now=datetime.now(timezone.utc))
    assert waiting["items"] == [] and row_of(waiting, held, "EXPORT")["reasons"]["grace_wait"] == 30 and waiting["identity_ok"]
    ign = backfill.compute(w.f, cfg, w.ledger(), sources={held}, now=datetime.now(timezone.utc), ignore_grace=True)
    assert len(asins_of(ign["items"])) == 30
    # 絞り込み: 周期の状態・最小ASIN数・周期
    assert not run(w, cfg, [held], states={"OPEN"})["rows"] and run(w, cfg, [held], states={"COMPLETE"})["rows"]
    assert not run(w, cfg, [held], min_asins=31)["rows"] and run(w, cfg, [held], min_asins=30)["rows"]
    ck = row_of(res, held, "EXPORT")["cycle_key"]
    assert run(w, cfg, [held], cycles={ck})["rows"] and not run(w, cfg, [held], cycles={"c999999"})["rows"]


def test_open_cycle_remainder_waits_for_the_grace_period_and_a_running_cycle_is_never_planned(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool("B5O", 9000)
    sid = w.source(asins, dirs=("EXPORT",), hold="ON_HOLD", per=3000, fetch_pages=2)  # 3ページ中2ページ=6,000件で打ち切り → OPEN（続きの取得は保留で止まっている）
    cfg = w.cfg()
    now = datetime.now(timezone.utc)
    wait = backfill.compute(w.f, cfg, w.ledger(), sources={sid}, now=now)  # 猶予内: 計画しない（件数は猶予待ちとして集計）
    r = row_of(wait, sid, "EXPORT")
    assert r["cycle_state"] == "OPEN" and r["reasons"]["grace_wait"] == 6000 and wait["items"] == []
    done = run(w, cfg, [sid])  # 猶予後: OPENでも端数まで全件を計画する
    assert [e["item"].asin_count for e in done["items"]] == [5000, 1000] and asins_of(done["items"]) == asins[:6000]
    assert row_of(done, sid, "EXPORT")["identity_ok"] and run(w, cfg, [sid], states={"OPEN"})["rows"]
    # 実行中のrun（RUNNING）がある周期は、猶予を無視しても計画しない
    sid2 = w.source(pool("B5P", 100), dirs=("EXPORT",), hold=None, per=40, fetch_pages=1)  # 続きのある一巡（OPEN）
    runs.claim("US", sid2)  # 続きの取得を開始して取得中のまま（RUNNING）
    busy = backfill.compute(w.f, cfg, w.ledger(), sources={sid2}, now=LATER, ignore_grace=True)
    rows2 = [x for x in busy["rows"] if x["source_id"] == sid2]
    assert any(x["reasons"]["run_in_progress"] for x in rows2) and busy["items"] == [] and all(x["identity_ok"] for x in rows2)


def test_open_cycle_whose_later_run_failed_is_reclaimed_from_the_last_usable_run(app_role_factory, registry, runs, fast_cfg, tmp_path):
    """実データの型: 使える取得(OPEN)の後に、続きの取得がCAPTCHAなどで失敗して保留になり、周期はBROKENにならずOPENのまま。watchの候補は無いが、保存済みの6,000件は回収する。
    失敗runの保存分（ここでは0件）は入力にしない。"""
    w = W(app_role_factory, registry, runs, tmp_path)
    asins = pool("B5F", 9000)
    i = next(w.n)
    key = unique_key("ABF")
    rp = w.reg.register_path("US", "SELLER", key, origin_import_id=w.us, origin_asin=f"B0BFU{i:05d}")
    sid = rp.source.source_id
    site = Site(paged(asins, 3000), source_url("US", load_source(w.f, "US", sid)))
    go(runs, w.f, site, sid, 2)  # 6,000件でOPEN
    site.captcha_pages = {3}
    try:
        go(runs, w.f, site, sid, 2)  # 続きがCAPTCHAで失敗（FAILED。周期はOPENのまま）
    except Exception:  # noqa: BLE001
        pass
    w.reg.set_status("US", sid, "ON_HOLD", "テスト")
    with app_role_factory.get_session("US") as conn, conn.begin():
        st = conn.execute(text("SELECT status, outcome, cycle_state FROM collector_run WHERE source_id=:s ORDER BY id"), {"s": sid}).all()
    assert [tuple(x) for x in st][-1][0] == "FAILED" and st[-1][2] == "OPEN"
    cfg = w.cfg()
    from workflow.buffer_recovery import candidate_runs
    assert [r for r in candidate_runs(w.f, cfg, "US")[1] if r[1] == sid] == []  # watchの候補は無い（後続の失敗runがあるが、BROKENではない）
    res = run(w, cfg, [sid])
    assert asins_of(res["items"]) == asins[:6000] and [e["item"].asin_count for e in res["items"]] == [5000, 1000] and res["identity_ok"]
    assert row_of(res, sid, "EXPORT")["reasons"]["register"] == 6000 and not res["errors"]


# ---------------------------------------------------------------- 依頼B: ON_HOLD出典の保存済みASINが通常の計画に入る

def test_watch_planning_includes_on_hold_sources_but_not_excluded_ones_and_leaves_the_hold_alone(app_role_factory, registry, runs, fast_cfg, tmp_path):
    w = W(app_role_factory, registry, runs, tmp_path)
    held = w.source(pool("B5P", 7000), dirs=("EXPORT",), hold="ON_HOLD")
    excl = w.source(pool("B5Q", 7000), dirs=("EXPORT",), hold="EXCLUDED")
    blocked = w.source(pool("B5R", 7000), dirs=("EXPORT",), hold="ON_HOLD")
    with app_role_factory.get_session("US") as conn, conn.begin():  # 永久除外のセラー（storefront_blocked_seller）の保留出典
        key = conn.execute(text("SELECT key_primary FROM acquisition_source WHERE source_id=:s"), {"s": blocked}).scalar_one()
    from sqlalchemy import create_engine
    import os
    eng = create_engine(os.environ["SEEDASIN_TEST_SUPERUSER_DATABASE_URL"])
    try:
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:k, 'PERMANENTLY_EXCLUDED') ON CONFLICT DO NOTHING"), {"k": key})
    finally:
        eng.dispose()
    cfg = w.cfg()
    before = {s: w.status(s) for s in (held, excl, blocked)}
    ledger = w.ledger()
    store = WorkflowStore(tmp_path / "wf")
    n, asins = stage_stalled_inputs(app_role_factory, cfg, store, ledger, now=LATER)
    sources_planned = {s.manifest["source_id"] for s in ledger.all_states().values()}
    assert held in sources_planned and excl not in sources_planned and blocked not in sources_planned  # 保留は入る。除外・永久除外のセラーは入らない
    mine = [s for s in ledger.all_states().values() if s.manifest["source_id"] == held]
    assert sorted(s.manifest["asin_count"] for s in mine) == [2000, 5000]
    assert {s: w.status(s) for s in (held, excl, blocked)} == before  # 保留・除外の状態は変えない
    assert stage_stalled_inputs(app_role_factory, cfg, store, ledger, now=LATER) == (0, 0)  # 冪等
    # 取得側の判定は変わらない: 保留のままで、取得できない（ON_HOLDのブロッカー）
    from acquisition.eligibility import db_now, decide, load_configs, load_facts
    with app_role_factory.get_session("US") as conn, conn.begin():
        f = load_facts(conn, "US", [held])[0]
        d = decide(f, load_configs(conn, "US")[f.source_type], db_now(conn))
    assert d.state == "ON_HOLD" and "ON_HOLD" in [b.code for b in d.blockers]


def test_engine_plans_saved_asins_of_an_on_hold_branch_without_changing_the_branch_or_the_hold(app_role_factory, registry, runs, fast_cfg, tmp_path, own):
    """engineの通常計画（expand）: 保留(ON_HOLD)の枝は取得しない（枝はSKIPPED_POLICYのまま・要解除）が、保存済みの結果は投入計画に回す。EXCLUDEDは回さない。"""
    from tests.integration.test_workflow import World, uid

    w = World(app_role_factory, tmp_path)
    rg = SourceRegistry(w.f, actor="tester")
    ids = {}
    for tag, status in (("held", "ON_HOLD"), ("excl", "EXCLUDED")):
        seller = uid("AEH" + tag[0].upper())
        rp = rg.register_path("US", "SELLER", seller, "", origin_import_id=w.us_import, origin_asin=w.us_top)
        sid = rp.source.source_id
        site = Site(paged(pool("B5" + tag[0].upper(), 40), 40), source_url("US", load_source(w.f, "US", sid)))
        go(make_runs(w.f), w.f, site, sid, 9)
        rg.set_status("US", sid, status, "テスト")
        ids[tag] = sid
    eng = w.engine()
    eng.new_state()
    acq = eng.st.tasks["acquire:US"]
    eng._acquire_sources(acq, "US", w.us_import, [{"source_id": s, "market": "US", "source_type": "SELLER", "key": None} for s in ids.values()])
    recs = acq.result["sources"]
    h, x = recs[str(ids["held"])], recs[str(ids["excl"])]
    assert h["mode"] == x["mode"] == "SKIPPED_POLICY" and h["needs_release"] and "saved_run_id" in h and "saved_run_id" not in x  # 枝の状態は従来どおり
    assert not any(isinstance(r.get("run_id"), int) for r in recs.values())  # 取得していない
    eng._run_expand(eng.st.tasks["expand:US"])
    ledger = eng.ledger
    planned = {s.manifest["source_id"]: s for s in ledger.all_states().values()}
    assert ids["held"] in planned and ids["excl"] not in planned and planned[ids["held"]].manifest["asin_count"] == 40
    assert eng.st.tasks["expand:US"].status == "DONE"
    with app_role_factory.get_session("US") as conn, conn.begin():
        sts = {r[0]: r[1] for r in conn.execute(text("SELECT source_id, status FROM acquisition_source WHERE source_id = ANY(:i)"), {"i": list(ids.values())})}
    assert sts == {ids["held"]: "ON_HOLD", ids["excl"]: "EXCLUDED"}  # 保留・除外は変わらない
