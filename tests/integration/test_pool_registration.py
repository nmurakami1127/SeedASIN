"""保存済みASINのプール登録（tool_submission/pool.py。docs/DEVIATIONS.md #106）の結合テスト。専用test-db・台帳はファイル。外部接続・実統合ツールなし。

確認すること: 5,001/12,300/20,000件の分割と全件一意・保存順／端数／再実行で0件／占有状態（UNKNOWN・AMBIGUOUS・ABANDONED・取り下げ・NOT_SUBMITTED・LINKED・repeat）の
不復活／他の出典で登録済みでも出る／IMPORT・EXPORT分離とレーン経路／除外出典・ON_HOLDの扱い／RUNNING・FAILEDのrunのASINも対象／時間ルール／
1出典の失敗が他を止めない／恒等式／台帳が追記のみ／watch・登録ループ・CLIから呼ばれる／RLS（US/JP分離）／pool-history-check。
"""
from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from acquisition.registry import SourceRegistry
from tests.integration.acq_helpers import make_active_report, unique_key
from tool_submission import pool
from tool_submission.cli import main as cli_main
from tool_submission.ledger import SubmissionLedger
from workflow.config import config_from_dict
from workflow.store import WorkflowStore

pytestmark = pytest.mark.integration

def _now() -> datetime:
    return datetime.now(timezone.utc)  # DBの時刻はテスト中に進むので、使う時点の現在時刻を取る


NOW = _now()  # watcherの擬似時計の基準だけに使う（DBの時刻との比較には使わない）


def asins(tag: str, n: int, start: int = 0) -> list[str]:
    """10桁の架空ASIN（B + 2桁タグ + 7桁連番）。"""
    return [f"B{tag}{i:07d}" for i in range(start, start + n)]


def insert_run(f, market: str, source_id: int, items: list[str], *, status="COMPLETED", outcome="SUCCESS_NONEMPTY", minutes_ago: float = 120) -> int:
    """終了済み（または実行中）のrunとそのASINを直接INSERTする（過去の時刻で作る。終了後のUPDATEは禁止されているため）。"""
    running = status in ("RUNNING", "CAPTCHA_WAIT")
    with f.get_session(market) as conn, conn.begin():
        rid = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, attempt_token, heartbeat_at, "
            "lease_expires_at, lease_seconds, outcome, completeness) VALUES (:mp, 'SELLER_STOREFRONT', 't', :st, "
            "clock_timestamp() - make_interval(mins => :m) - interval '5 minutes', "
            + ("NULL" if running else "clock_timestamp() - make_interval(mins => :m)") + ", :sid, gen_random_uuid(), clock_timestamp(), "
            "clock_timestamp() + interval '1 hour', 300, :o, :c) RETURNING id"),
            {"mp": market, "st": status, "m": minutes_ago, "sid": source_id, "o": None if running else outcome,
             "c": None if running else ("COMPLETE" if outcome.startswith("SUCCESS") else "PARTIAL")}).scalar_one()
        if items:
            conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES (:mp, :r, :a)"),
                         [{"mp": market, "r": rid, "a": a} for a in items])
    return rid


class Env:
    """US/JPのレポートと、レーン設定。USレーン: origin=US(+extra=JP)、JPレーン: origin=JP。"""

    def __init__(self, f, tmp_path, *, extra_us=True, jp_lane=True, **cfg_extra) -> None:
        self.f, self.tmp = f, tmp_path
        self.registry = SourceRegistry(f, actor="tester")
        tag = uuid.uuid4().hex[:3].upper()
        self.us = make_active_report(f, tmp_path, "US", [(f"B0PL{tag}U01", 80, 4.0, 7)], name=f"u{tag}.csv")
        self.jp = make_active_report(f, tmp_path, "JP", [(f"B0PL{tag}J01", 80, 4.0, 3)], name=f"j{tag}.csv")
        self.us_asin, self.jp_asin = f"B0PL{tag}U01", f"B0PL{tag}J01"
        lanes = {"US": {"origin_import": self.us, "extra_origin_imports": [self.jp] if extra_us else []}}
        if jp_lane:
            lanes["JP"] = {"origin_import": self.jp}
        self.cfg = config_from_dict({"lanes": lanes, "submission_dir": str(tmp_path / "sub"), "state_dir": str(tmp_path / "wf"), **cfg_extra})
        self.ledger = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")

    def source(self, market="US", *, export=True, import_=False, jp_origin_in_jp=False) -> int:
        """出典を作り、発見経路を付ける（USレポート起点=EXPORT、JPレポート起点=IMPORT）。"""
        key = unique_key("PL")
        rp = None
        if export:
            rp = self.registry.register_path(market, "SELLER", key, origin_import_id=self.us, origin_asin=self.us_asin)
        if import_:
            rp = self.registry.register_path(market, "SELLER", key, origin_import_id=self.jp, origin_asin=self.jp_asin)
        if rp is None:
            rp = self.registry.register_source(market, "SELLER", key)
            return rp.source_id
        return rp.source.source_id

    def compute(self, **kw):
        kw.setdefault("now", _now())
        return pool.compute(self.f, self.cfg, self.ledger, **kw)

    def plan(self, **kw):
        rep = self.compute(**kw)
        done, skipped = pool.write_items(rep["items"], self.ledger)
        return rep, done, skipped


def row_of(rep, sid, direction=None):
    return next(r for r in rep["rows"] if r["source_id"] == sid and (direction is None or r["direction"] == direction))


def listed(rep, sid=None):
    """計画された入力のASINの並び（保存順）。"""
    return [a for e in rep["items"] if sid is None or e["item"].source_id == sid for a in e["content"].decode().split()]


def ok(rep):
    assert rep["identity_ok"], [r for r in rep["rows"] if not r["identity_ok"]]


@pytest.fixture()
def env(app_role_factory, tmp_path):
    return Env(app_role_factory, tmp_path)


# ------------------------------------------------------------------ 分割・端数・再実行

@pytest.mark.parametrize("n,expected", [(5001, [5000, 1]), (12300, [5000, 5000, 2300]), (20000, [5000, 5000, 5000, 5000])])
def test_split_into_inputs_of_at_most_5000_keeps_order_covers_everything_and_a_rerun_plans_nothing(env, n, expected):
    sid = env.source()
    items = asins("S1", n)
    insert_run(env.f, "US", sid, items)
    rep, done, skipped = env.plan()
    ok(rep)
    assert [e["item"].asin_count for e in rep["items"]] == expected and not skipped and len(done) == len(expected)
    assert listed(rep) == items  # 保存順のまま・全件・重複なし
    assert len(set(listed(rep))) == n
    assert {e["item"].direction for e in rep["items"]} == {"EXPORT"}
    assert row_of(rep, sid)["reasons"]["register"] == n and row_of(rep, sid)["inputs"] == len(expected)
    again = env.compute()
    ok(again)
    assert again["items"] == [] and row_of(again, sid)["reasons"]["registered_same_source"] == n  # 再実行で0件
    assert len(env.ledger.all_states()) == len(expected)  # 同一鍵の二重計画なし


def test_a_small_remainder_is_planned_without_waiting_for_5000(env):
    sid = env.source()
    insert_run(env.f, "US", sid, asins("S2", 37))
    rep, done, _ = env.plan()
    assert [e["item"].asin_count for e in rep["items"]] == [37] and len(done) == 1
    # 同じ計画を二重に書こうとしても、台帳の鍵が既にあるので書かない
    _, again = pool.write_items(rep["items"], env.ledger)
    assert again and "台帳に既にある鍵" in again[0]["reason"] and len(env.ledger.all_states()) == 1


def test_asins_added_later_are_planned_as_a_new_input_and_the_ledger_only_grows(env):
    sid = env.source()
    r1 = insert_run(env.f, "US", sid, asins("S3", 10))
    rep1, _, _ = env.plan()
    assert rep1["items"][0]["item"].collector_run_id == r1  # 入力に寄与した最新のrun
    before = env.ledger.path.read_text(encoding="utf-8")
    r2 = insert_run(env.f, "US", sid, asins("S3", 7, start=10), minutes_ago=100)
    rep, done, _ = env.plan()
    assert listed(rep) == asins("S3", 7, start=10) and len(done) == 1 and rep["items"][0]["item"].collector_run_id == r2
    assert env.ledger.path.read_text(encoding="utf-8").startswith(before)  # 既存の行は変わらない（追記のみ）
    ok(rep)


# ------------------------------------------------------------------ 占有状態を再計画しない

def test_occupied_states_are_never_replanned_and_withdrawn_or_not_submitted_are_not_revived(env):
    sid = env.source()
    led = env.ledger
    groups: dict[str, list[str]] = {}
    keys: dict[str, str] = {}
    for i, name in enumerate(("planned", "unknown", "ambiguous", "abandoned", "linked", "repeat", "not_submitted", "withdrawn", "dup_withdrawn", "submitting", "memo_saved")):
        g = asins(f"G{chr(65 + i)}", 10)
        insert_run(env.f, "US", sid, g)
        rep, done, _ = env.plan()
        assert listed(rep) == g, name
        key = done[0]["item"].key
        groups[name], keys[name] = g, key
        if name == "repeat":  # 過去の一巡で投入済みのASINだけのrepeat計画（71本相当）。自動では送信・復活させない
            it = dataclasses.replace(done[0]["item"], repeat_of_earlier_cycle=True)
            led.append(key, "PLANNED", manifest={**dataclasses.asdict(it), "status": "READY"})
        elif name != "planned":
            if name == "withdrawn":
                led.append(key, "PLAN_WITHDRAWN", reason="t")
            elif name == "dup_withdrawn":
                led.withdraw_duplicate(key, evidence_run_id="1", reason="t", memo="m")
            else:
                led.append(key, "SUBMIT_STARTED", before_run_ids=[1], options={})
                if name == "unknown":
                    led.append(key, "OUTCOME_UNKNOWN", reason="t")
                elif name == "ambiguous":
                    led.append(key, "AMBIGUOUS", reason="t")
                elif name == "abandoned":
                    led.append(key, "OUTCOME_UNKNOWN", reason="t")
                    led.abandon_unknown(key, reason="t")
                elif name == "linked":
                    led.append(key, "RUN_IDENTIFIED", tool_run_id="9001")
                    led.append(key, "MEMO_SAVED")
                    led.append(key, "LINKED")
                elif name == "memo_saved":
                    led.append(key, "RUN_IDENTIFIED", tool_run_id="9002")
                    led.append(key, "MEMO_SAVED")
                elif name == "not_submitted":
                    led.append(key, "NOT_SUBMITTED_VERIFIED", reason="t")
    st = led.all_states()
    assert [st[keys[n]].state for n in groups] == ["PLANNED", "UNKNOWN", "AMBIGUOUS", "ABANDONED", "LINKED", "PLANNED", "NOT_SUBMITTED", "WITHDRAWN", "WITHDRAWN",
                                                   "SUBMITTING", "MEMO_SAVED"]
    new = asins("NW", 10) + ["BADASIN"]
    insert_run(env.f, "US", sid, new)
    before = led.path.read_text(encoding="utf-8")
    rep, done, _ = env.plan()
    ok(rep)
    assert listed(rep) == asins("NW", 10)  # 新しいASINだけ
    r = row_of(rep, sid)["reasons"]
    assert (r["register"], r["registered_same_source"], r["not_submitted"], r["withdrawn"], r["invalid_format"]) == (10, 80, 10, 20, 1)
    assert led.path.read_text(encoding="utf-8").startswith(before)
    # 状態は変わらない（再送・復活・状態変更をしない）
    st2 = led.all_states()
    assert [st2[keys[n]].state for n in groups] == [st[keys[n]].state for n in groups]
    assert st2[keys["dup_withdrawn"]].duplicate_withdrawn and st2[keys["repeat"]].manifest["repeat_of_earlier_cycle"]


# ------------------------------------------------------------------ 他の出典・方向・レーン

def test_asins_registered_under_another_source_are_still_planned_and_counted_as_overlap(env):
    a, b = env.source(), env.source()
    shared = asins("OV", 30)
    insert_run(env.f, "US", a, shared)
    env.plan()
    insert_run(env.f, "US", b, shared + asins("OW", 5))
    rep, done, _ = env.plan()
    ok(rep)
    assert listed(rep, b) == shared + asins("OW", 5) and listed(rep, a) == []  # 他の出典で登録済みでも除外しない
    assert row_of(rep, b)["overlap_other"] == 30  # 情報のみ


def test_directions_are_separate_and_a_direction_without_a_lane_path_is_not_planned(app_role_factory, tmp_path):
    both = Env(app_role_factory, tmp_path / "a")
    sid = both.source(export=True, import_=True)
    g = asins("DR", 12)
    insert_run(both.f, "US", sid, g)
    rep, done, _ = both.plan()
    ok(rep)
    assert sorted((e["item"].direction, e["item"].business_market, e["item"].memo.split("_")[0], e["item"].asin_count) for e in rep["items"]) == \
        [("EXPORT", "US", "輸出", 12), ("IMPORT", "JP", "輸入", 12)]  # 混ぜない
    # 登録済みの方向は他方の方向に影響しない
    assert row_of(both.compute(), sid, "EXPORT")["reasons"]["registered_same_source"] == 12
    # USレーンのextra（JPレポート）が無ければ、IMPORT（JPレポート起点）の経路はレーンに無い → IMPORTは出さない
    only = Env(app_role_factory, tmp_path / "b", extra_us=False, jp_lane=False)
    sid2 = only.source(export=True, import_=True)
    insert_run(only.f, "US", sid2, asins("DS", 8))
    rep2, _, _ = only.plan()
    ok(rep2)
    assert {e["item"].direction for e in rep2["items"]} == {"EXPORT"}
    assert row_of(rep2, sid2, "IMPORT")["reasons"]["no_lane_path"] == 8
    # JP市場の出典: JPレーンが無ければ何も出さない
    sid3 = only.source("JP", export=False, import_=True)
    insert_run(only.f, "JP", sid3, asins("DJ", 8))
    rep3 = only.compute(markets=["JP"])
    ok(rep3)
    assert rep3["items"] == [] and row_of(rep3, sid3)["reasons"]["no_lane_path"] == 8


def test_excluded_permanently_excluded_and_no_direction_sources_are_not_planned_but_on_hold_is(env):
    held, manual, excl, perm, nodir = (env.source() for _ in range(5))
    for sid in (held, manual, excl, perm, nodir):
        insert_run(env.f, "US", sid, asins(f"X{sid % 10}", 6))
    env.registry.set_status("US", held, "ON_HOLD", "テスト")  # 手動保留（取得側の判断）は対象に含める
    env.registry.set_status("US", excl, "EXCLUDED", "テスト")
    with env.f.get_session("US") as conn, conn.begin():
        key = conn.execute(text("SELECT key_primary FROM acquisition_source WHERE source_id = :s"), {"s": perm}).scalar_one()
        conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:k, 'PERMANENTLY_EXCLUDED')"), {"k": key})
    # 方向なし: 経路が手動のみの出典
    key2 = unique_key("PL")
    nd = env.registry.register_path("US", "SELLER", key2).source.source_id
    insert_run(env.f, "US", nd, asins("ND", 6))
    rep = env.compute()
    ok(rep)
    assert {e["item"].source_id for e in rep["items"]} == {held, manual, nodir}
    assert row_of(rep, excl)["reasons"]["source_excluded"] == 6 and row_of(rep, perm)["reasons"]["source_excluded"] == 6
    assert row_of(rep, nd)["reasons"]["no_direction"] == 6 and row_of(rep, nd)["direction"] is None


def test_asins_of_running_and_failed_runs_are_included_and_marked_partial(env):
    sid = env.source()
    insert_run(env.f, "US", sid, asins("RF", 5), status="FAILED", outcome="FAILED", minutes_ago=500)
    insert_run(env.f, "US", sid, asins("RG", 5), status="COMPLETED", outcome="SUCCESS_TRUNCATED", minutes_ago=500)
    insert_run(env.f, "US", sid, asins("RH", 5), status="RUNNING", minutes_ago=1)  # 実行中=現在扱い（最古が500分前なのでmax_waitで計画される）
    rep = env.compute()
    ok(rep)
    assert listed(rep) == asins("RF", 5) + asins("RG", 5) + asins("RH", 5)
    assert all(e["item"].input_is_partial and e["item"].partial_reason for e in rep["items"])


# ------------------------------------------------------------------ 時間ルール

def test_time_rule_quiet_max_wait_waiting_and_disabled(app_role_factory, tmp_path):
    def reg(quiet, max_wait, runs, tag, running=False):
        e = Env(app_role_factory, tmp_path / tag, pool_quiet_minutes=quiet, pool_max_wait_minutes=max_wait)
        sid = e.source()
        for i, m in enumerate(runs):
            insert_run(e.f, "US", sid, asins(tag[:2].upper(), 3, start=i * 3), minutes_ago=m)
        if running:
            insert_run(e.f, "US", sid, asins(tag[:2].upper(), 3, start=90), status="RUNNING", minutes_ago=400)
        rep = e.compute()
        ok(rep)
        return row_of(rep, sid), rep

    r, rep = reg(30, 360, [10], "w1")  # 最新が10分前 → 待つ
    assert r["reasons"]["time_wait"] == 3 and r["reasons"]["register"] == 0 and rep["items"] == [] and r["wait_until"]
    r, rep = reg(30, 360, [31], "w2")  # 静かな時間が経過 → 計画
    assert r["reasons"]["register"] == 3
    r, rep = reg(30, 360, [400, 5], "w3")  # 追加が続いていても、最古が360分を超えたので全件を計画
    assert r["reasons"]["register"] == 6
    r, rep = reg(30, 360, [300, 5], "w4")  # 最古300分・最新5分 → まだ待つ
    assert r["reasons"]["time_wait"] == 6
    r, rep = reg(0, 360, [1], "w5")  # quiet=0（待たない）
    assert r["reasons"]["register"] == 3
    r, rep = reg(30, 0, [400, 5], "w6")  # max_wait=0=その待ちを使わない → quietだけ（最新5分なので待つ）
    assert r["reasons"]["time_wait"] == 6
    r, rep = reg(30, 360, [], "w7", running=True)  # 実行中のrunのASINは「現在」扱い → 待つ
    assert r["reasons"]["time_wait"] == 3


# ------------------------------------------------------------------ 失敗の分離・RLS

def test_one_source_failure_does_not_stop_the_others_and_is_reported(env):
    bad, good = env.source(), env.source()
    insert_run(env.f, "US", bad, asins("BD", 4))
    insert_run(env.f, "US", good, asins("GD", 4))
    # badの出典・方向に、入力ファイルが読めない計画を台帳に置く（二重計画を避けるため、その出典・方向は計画しない）
    env.ledger.append("deadbeef00000001", "PLANNED", manifest={"key": "deadbeef00000001", "fetch_market": "US", "source_id": bad, "direction": "EXPORT",
                                                              "asin_count": 4, "input_file": str(env.tmp / "missing.csv")})
    rep = env.compute()
    ok(rep)
    assert {e["item"].source_id for e in rep["items"]} == {good}
    assert [(e["source_id"], e["direction"]) for e in rep["errors"]] == [(bad, "EXPORT")] and row_of(rep, bad)["error"]
    assert row_of(rep, bad)["reasons"]["error"] == 4
    assert any("見送り" in x or "失敗" in x for x in pool.format_report(rep))


def test_us_and_jp_are_separated_by_rls(env):
    us_src = env.source("US")
    jp_src = env.source("JP", export=False, import_=True)
    insert_run(env.f, "US", us_src, asins("RU", 4))
    insert_run(env.f, "JP", jp_src, asins("RJ", 4))
    rep_jp = env.compute(markets=["JP"])
    rep_us = env.compute(markets=["US"])
    assert {r["market"] for r in rep_jp["rows"]} == {"JP"} and {r["market"] for r in rep_us["rows"]} == {"US"}
    assert any(r["source_id"] == jp_src for r in rep_jp["rows"]) and any(r["source_id"] == us_src for r in rep_us["rows"])
    assert set(asins("RU", 4)) & set(listed(rep_jp)) == set() and set(asins("RJ", 4)) & set(listed(rep_us)) == set()  # 他方の市場のASINは見えない
    assert listed(rep_jp, jp_src) == asins("RJ", 4) and listed(rep_us, us_src) == asins("RU", 4)
    assert {e["item"].fetch_market for e in rep_jp["items"]} == {"JP"}
    # 区画を設定しない接続ではCollectorのASINを読めない（読み取りは必ずget_session(market)経由）
    with env.f.get_session("JP") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM collector_asin WHERE asin LIKE 'BRU%'")).scalar_one() == 0


# ------------------------------------------------------------------ 呼び出し元（watch・登録ループ・CLI）

def test_registration_loop_plans_through_the_pool_and_only_keys_does_not(env, monkeypatch):
    from workflow import registration_loop as rl

    sid = env.source()
    insert_run(env.f, "US", sid, asins("LP", 23))
    monkeypatch.setattr(rl, "register_saved", lambda *a, **k: type("S", (), {"tasks": {}})())
    said: list[str] = []
    res = rl.run_cycle(env.cfg, env.f, None, now=_now(), say=said.append, only_keys={"x"})
    assert res["planned"] == (0, 0) and not env.ledger.all_states()  # --only-keysは新規の計画を作らない
    res = rl.run_cycle(env.cfg, env.f, None, now=_now(), say=said.append)
    assert res["planned"] == (1, 23) and res["error"] is None
    st = next(iter(env.ledger.all_states().values()))
    assert st.state == "PLANNED" and st.manifest["asin_count"] == 23 and st.manifest["cycle_key"] == "pool"
    assert rl.run_cycle(env.cfg, env.f, None, now=_now(), say=said.append)["planned"] == (0, 0)  # 冪等


def test_watch_stage_creates_submit_tasks_once(env):
    sid = env.source()
    insert_run(env.f, "US", sid, asins("WT", 9))
    store = WorkflowStore(env.cfg.state_dir)
    assert pool.stage_pool_inputs(env.f, env.cfg, store, env.ledger, now=_now()) == (1, 9)
    assert pool.stage_pool_inputs(env.f, env.cfg, store, env.ledger, now=_now()) == (0, 0)
    (wid,) = store.list_ids()
    tasks = store.load(wid).tasks
    key = next(iter(env.ledger.all_states()))
    assert list(tasks) == [f"submit:{key}"] and tasks[f"submit:{key}"].result["asin_count"] == 9 and tasks[f"submit:{key}"].lane == "US"


def test_watcher_cycle_calls_the_pool_every_15_minutes_instead_of_the_cycle_dependent_staging(env, monkeypatch):
    from types import SimpleNamespace

    import workflow.buffer_recovery as br
    from workflow.watch import Watcher

    calls: list = []
    monkeypatch.setattr("tool_submission.pool.stage_pool_inputs", lambda *a, **k: calls.append(k["now"]) or (0, 0))
    monkeypatch.setattr(br, "stage_stalled_inputs", lambda *a, **k: pytest.fail("旧方式の端数持ち越しは呼ばれない"))
    t = [NOW]
    w = Watcher(env.cfg, env.f, SimpleNamespace(), now=lambda: t[0], sleep=lambda s: None, out=lambda x: None)
    monkeypatch.setattr(w, "_maintain_tool_session", lambda: None)
    w.cycle()
    assert calls == []  # 起動直後は15分後から
    t[0] = NOW + timedelta(minutes=16)
    w.cycle()
    assert len(calls) == 1
    t[0] = NOW + timedelta(minutes=20)
    w.cycle()
    assert len(calls) == 1  # 15分の間隔


def test_cli_plan_pool_dry_run_writes_nothing_and_apply_needs_confirmation(env, tmp_path, capsys):
    sid = env.source()
    insert_run(env.f, "US", sid, asins("CL", 5001))
    cfgp = tmp_path / "wf.json"
    cfgp.write_text(json.dumps({"lanes": {"US": {"origin_import": env.us, "extra_origin_imports": [env.jp]}, "JP": {"origin_import": env.jp}},
                                "submission_dir": str(tmp_path / "sub2")}), encoding="utf-8")
    base = ["plan-pool", "--config", str(cfgp), "--market", "US", "--now", _now().isoformat(), "--report-dir", str(tmp_path / "rep")]
    assert cli_main(base, factory=env.f) == 0
    out = capsys.readouterr().out
    assert "読み取りのみ" in out and "恒等式" in out and "成立" in out
    assert not (tmp_path / "sub2").exists()  # 台帳・入力ファイルは作らない
    assert list((tmp_path / "rep").glob("pool_dryrun_*.json"))
    assert cli_main([*base, "--apply"], factory=env.f, confirm=lambda q: False) == 2
    assert not (tmp_path / "sub2").exists()
    assert cli_main([*base, "--apply"], factory=env.f, confirm=lambda q: True) == 0
    led = SubmissionLedger(tmp_path / "sub2" / "ledger.jsonl")
    assert sorted(int(s.manifest["asin_count"]) for s in led.all_states().values()) == [1, 5000]
    assert all(s.state == "PLANNED" for s in led.all_states().values())
    assert cli_main([*base, "--apply"], factory=env.f, confirm=lambda q: True) == 0  # 再実行は0件
    assert "追記する新しい入力はありません" in capsys.readouterr().out
    assert list((tmp_path / "rep").glob("pool_apply_*.json"))


def test_pool_history_check_counts_targets_already_in_tool_output_history(env):
    sid = env.source()
    g = asins("HC", 10)
    insert_run(env.f, "US", sid, g)
    rep = env.compute()
    key = rep["items"][0]["item"].source_key
    with env.f.get_session("US") as conn, conn.begin():
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', :k, 'T-PH1', NULL)"), {"k": key})
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', 'OTHERSELLER', 'T-PH2', NULL)"))
        for tr, a in (("T-PH1", g[0]), ("T-PH1", g[1]), ("T-PH2", g[2]), ("T-PH2", g[0])):
            conn.execute(text("INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) VALUES ('US', :t, :a, now())"), {"t": tr, "a": a})
    (r,) = pool.history_check(env.f, rep)
    assert (r["targets"], r["in_output_same_source"], r["in_output_any_source"], r["source_link_count"], r["business_market"]) == (10, 2, 3, 1, "US")
    # 読み取り専用: 台帳・入力ファイルは作らない
    assert not env.ledger.path.exists()
