"""workflowの完了管理（持ち越し・引き渡し・整理・成果表示）の単体テスト。DB・ブラウザ・実データ不要（合成データ・一時ディレクトリのみ）。
docs/DEVIATIONS.md #97。"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tool_submission.ledger import SubmissionLedger
from tool_submission.ledger_read import started_in_window
from workflow import carryover, progress as P
from workflow.carryover import close_carried, handoff_submit_tasks, lane_is_blocked
from workflow.reconcile import apply_reconcile, format_scan, scan
from workflow.store import State, Task, WorkflowStore

NOW = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)


def prog(tmp_path, now=NOW):
    return P.SourceProgress(tmp_path / "source_progress.jsonl", now=lambda: now)


# ---------------------------------------------------------------- 分類（古い記録との後方互換を含む）

@pytest.mark.parametrize("rec,kind", [
    ({"mode": "HELD", "reason": "取得ページの上限（200）に達しています"}, P.PAUSED_PAGE_CAP),            # 古い状態ファイル（pauseなし）
    ({"mode": "HELD", "pause": P.PAUSED_PAGE_CAP, "reason": "x"}, P.PAUSED_PAGE_CAP),
    ({"mode": "HELD", "daily": True, "reason": "x"}, P.PAUSED_DAILY_LIMIT),
    ({"mode": "HELD", "retry_at": "2026-11-05T00:00:00Z", "reason": "COOLDOWN"}, P.WAIT_COOLDOWN),
    ({"mode": "HELD", "reason": "claimできませんでした"}, P.WAIT_CLAIM),
    ({"mode": "CAPTCHA"}, P.CAPTCHA),
    ({"mode": "FAILED", "error_code": "SERVER_ERROR"}, P.COMM_FAILURE),
    ({"mode": "FAILED", "error_code": "PAGE_LOAD_FAILED"}, P.COMM_FAILURE),
    ({"mode": "FAILED", "outcome": "LEASE_LOST"}, P.COMM_FAILURE),
    ({"mode": "PARTIAL_SAVED", "error_code": "NO_NEW_ASINS_LOOP"}, P.POSITION_INCONSISTENT),
    ({"mode": "FAILED", "error_code": "OVERLAP_MISMATCH"}, P.POSITION_INCONSISTENT),
    ({"mode": "FAILED", "error_code": "CURSOR_INVALID"}, P.POSITION_INCONSISTENT),
    ({"mode": "FAILED", "error_code": "SOMETHING"}, P.OTHER_FAILURE),
    ({"mode": "FETCHED"}, None), ({"mode": "REUSED"}, None), ({"mode": "EMPTY"}, None), ({"mode": "SKIPPED_POLICY"}, None),
])
def test_page_cap_is_a_separate_state_from_captcha_comm_failure_and_position_errors(rec, kind):
    assert P.classify(rec) == kind


def test_page_cap_is_a_normal_pause_not_a_failure_kind():
    assert P.PAUSED_PAGE_CAP in P.PAUSE_KINDS and P.PAUSED_PAGE_CAP not in P.FAILURE_KINDS and P.CAPTCHA not in P.PAUSE_KINDS
    assert len({P.PAUSED_PAGE_CAP, P.CAPTCHA, P.COMM_FAILURE, P.POSITION_INCONSISTENT}) == 4


# ---------------------------------------------------------------- 出典単位の進行台帳

def test_progress_folds_acquiring_acquired_and_carry(tmp_path):
    p = prog(tmp_path)
    p.append("US", 5, "ACQUIRING", wf="wf-1", pid=os.getpid(), plan="FRESH", start_page=1)
    assert p.state("US", 5).status == "ACQUIRING"
    p.append("US", 5, "ACQUIRED", wf="wf-1", pages=60, start_page=1, outcome="SUCCESS_TRUNCATED", run_id=9)
    st = p.state("US", 5)
    assert st.status == "ACQUIRED" and st.last_range["end_page"] == 60
    p.carry("US", 5, P.PAUSED_PAGE_CAP, wf="wf-1", task="acquire:US", reason="cap")
    assert p.state("US", 5).status == "RESUME_WAITING" and p.state("US", 5).carry["count"] == 1
    assert p.carry("US", 5, P.PAUSED_PAGE_CAP, wf="wf-1", task="acquire:US", reason="cap") is None   # 冪等
    p.resolve("US", 5, wf="wf-2", reason="REUSED")
    assert p.state("US", 5).status == "ACQUIRED" and p.state("US", 5).carry is None
    assert p.resolve("US", 5, wf="wf-2", reason="x") is None


def test_due_carried_respects_retry_time_lane_and_never_picks_captcha(tmp_path):
    p = prog(tmp_path)
    p.carry("US", 1, P.PAUSED_PAGE_CAP, wf="w", task="acquire:US", reason="", lane="US")
    p.carry("US", 2, P.PAUSED_DAILY_LIMIT, wf="w", task="acquire:US", reason="", rec={"retry_at": "2026-11-04T15:00:00Z"}, lane="US")
    p.carry("US", 3, P.CAPTCHA, wf="w", task="acquire:US", reason="", lane="US")
    p.carry("US", 4, P.PAUSED_PAGE_CAP, wf="w", task="acquire:JP", reason="", lane="JP")
    assert p.due_carried("US", lane="US") == [1]                       # 2は時刻前、3はCAPTCHA（人手）、4は別のlane
    later = prog(tmp_path, NOW + timedelta(hours=13))
    assert sorted(later.due_carried("US", lane="US")) == [1, 2]


def test_failure_carry_backs_off_each_time():
    r1 = P.retry_after_for(P.COMM_FAILURE, {}, 0, NOW)
    r3 = P.retry_after_for(P.COMM_FAILURE, {}, 2, NOW)
    assert P.parse_iso(r1) - NOW == timedelta(minutes=30) and P.parse_iso(r3) - NOW == timedelta(minutes=120)
    assert P.parse_iso(P.retry_after_for(P.COMM_FAILURE, {}, 20, NOW)) - NOW == timedelta(hours=24)
    assert P.retry_after_for(P.PAUSED_PAGE_CAP, {}, 5, NOW) == P.iso(NOW)


def test_guard_blocks_only_a_live_concurrent_claim_and_never_a_normal_continue(tmp_path):
    p = prog(tmp_path)
    p.append("US", 7, "ACQUIRED", wf="w", pages=60, start_page=1, outcome="SUCCESS_TRUNCATED")
    # 正常な続き取得（前回の最後のページ+1）は止めない。先頭からの取り直しも、このguardでは止めない（範囲の重なりは計測のみ）
    assert p.guard("US", 7, plan="CONTINUE", resume_page_no=61) is None
    assert p.guard("US", 7, plan="RESTART_BROKEN", resume_page_no=None) is None
    assert p.range_overlap("US", 7, plan="CONTINUE", resume_page_no=61) is None
    ov = p.range_overlap("US", 7, plan="RESTART_BROKEN", resume_page_no=None)
    assert ov["planned_start"] == 1 and ov["prev_range"] == [1, 60]
    # 別プロセス(生きている)が取得中の記録 → WAIT_BUSY。持ち主が自分/死んでいれば無視
    p.append("US", 8, "ACQUIRING", wf="w", pid=1, plan="FRESH", start_page=1)
    g = p.guard("US", 8, plan="FRESH", resume_page_no=None, me=os.getpid())
    assert g and g["kind"] == P.WAIT_BUSY
    p.append("US", 9, "ACQUIRING", wf="w", pid=2 ** 22 + 12345, plan="FRESH", start_page=1)   # 存在しないpid
    assert p.guard("US", 9, plan="FRESH", resume_page_no=None) is None
    assert prog(tmp_path, NOW + timedelta(hours=7)).guard("US", 8, plan="FRESH", resume_page_no=None, me=os.getpid()) is None  # 古い記録


def test_restart_gate_defers_a_second_blind_restart_but_allows_the_first_and_small_cycles(tmp_path):
    p = prog(tmp_path)
    big = {"prior_pages": 60, "last_error": "REPEATED_PAGE", "restart_streak": 1}
    assert P.restart_gate(p, "US", 3, big) is None                       # 初回の取り直しは従来どおり
    p.append("US", 3, "ACQUIRING", wf="w", plan="RESTART_BROKEN", pid=1, start_page=1)
    g = P.restart_gate(p, "US", 3, big)
    assert g and g["kind"] == P.WAIT_RESTART_REVIEW and g["retry_at"] == P.iso(NOW + timedelta(hours=24))
    assert P.restart_gate(p, "US", 3, {"prior_pages": 5}) is None        # 小さい一巡は取り直しで失うものが少ない
    assert P.restart_gate(prog(tmp_path, NOW + timedelta(hours=25)), "US", 3, big) is None   # 時間が経てば取り直す（永久に止めない）


def test_low_yield_backs_off_with_reevaluation_date_and_recovers(tmp_path):
    p = prog(tmp_path)
    assert P.note_low_yield(p, "US", 4, wf="w", run_id=1, pages=5, total=100, new=0) is None       # ページ数が少ないと評価しない
    P.note_low_yield(p, "US", 4, wf="w", run_id=1, pages=60, total=1000, new=2)
    assert p.deprioritized("US") == {4} and p.state("US", 4).low_yield["level"] == 0
    P.note_low_yield(p, "US", 4, wf="w", run_id=2, pages=60, total=1000, new=3)
    assert p.state("US", 4).low_yield["level"] == 1                                        # 続くたびに後退が長くなる
    assert prog(tmp_path, NOW + timedelta(days=60)).deprioritized("US") == set()           # 期限が来れば再選択される（除外ではない）
    P.note_low_yield(p, "US", 4, wf="w", run_id=3, pages=60, total=1000, new=600)
    assert p.deprioritized("US") == set()                                                  # 効率が戻れば解除


# ---------------------------------------------------------------- 持ち越しで閉じる（直書きCOMPLETEDではない）

def acquire_task(recs, status="HELD", attempts=1):
    return Task("acquire:US", "acquire", "US", status=status, attempts=attempts, detail="保留", retry_after="2026-11-04T04:00:00Z",
                result={"market": "US", "sources": recs, "usable": 1})


def test_close_carried_records_reason_and_destination_and_keeps_fetched_results(tmp_path):
    p = prog(tmp_path)
    recs = {"1": {"mode": "FETCHED", "run_id": 10, "pages": 60}, "2": {"mode": "HELD", "reason": "取得ページの上限（200）に達しています"},
            "3": {"mode": "HELD", "daily": True, "reason": "日次"}}
    st = State("wf-1", "t", {}, "h")
    st.tasks["acquire:US"] = acquire_task(recs)
    st.tasks["expand:US"] = Task("expand:US", "expand", "US", status="DONE")
    ch = close_carried(st, p, NOW)
    t = st.tasks["acquire:US"]
    assert t.status == "DONE" and t.result["closed_reason"] == "CARRIED_OVER"
    assert t.result["closed_from"]["status"] == "HELD" and t.result["closed_from"]["retry_after"] == "2026-11-04T04:00:00Z"   # 元の状態を残す
    assert {c["kind"] for c in t.result["carried_over"]} == {P.PAUSED_PAGE_CAP, P.PAUSED_DAILY_LIMIT}
    assert recs["1"] == {"mode": "FETCHED", "run_id": 10, "pages": 60}                  # 取得成功分は他の保留に影響されない
    assert recs["2"]["carried"] is True and recs["2"]["mode"] == "HELD"                  # 未処理の記録は消さない
    assert p.state("US", 2).status == "RESUME_WAITING" and p.state("US", 3).carry["kind"] == P.PAUSED_DAILY_LIMIT
    assert len(ch) == 1 and any("CLOSED_CARRIED_OVER" in e["msg"] for e in st.events)
    assert close_carried(st, p, NOW) == []                                                # 冪等


def test_close_carried_never_hides_captcha_and_waits_for_failures_to_repeat(tmp_path):
    p = prog(tmp_path)
    for recs, attempts, closable in (
            ({"1": {"mode": "CAPTCHA"}, "2": {"mode": "HELD", "reason": "取得ページの上限"}}, 9, False),
            ({"1": {"mode": "FAILED", "error_code": "SERVER_ERROR"}}, 1, False),
            ({"1": {"mode": "FAILED", "error_code": "SERVER_ERROR"}}, 3, True),
            ({"1": {"mode": "PARTIAL_SAVED", "error_code": "NO_NEW_ASINS_LOOP"}}, 3, True)):
        st = State("wf-x", "t", {}, "h")
        st.tasks["acquire:US"] = acquire_task(recs, status="FAILED" if "FAILED" in json.dumps(recs) else "HELD", attempts=attempts)
        assert bool(close_carried(st, p, NOW, record=False)) is closable, recs
        assert st.tasks["acquire:US"].status != "DONE" or closable
    st = State("wf-w", "t", {}, "h")
    st.tasks["acquire:US"] = acquire_task({"1": {"mode": "HELD", "reason": "取得ページの上限"}}, status="WAITING")
    assert close_carried(st, p, NOW) == []                                                # 人の操作待ち(WAITING)は閉じない


# ---------------------------------------------------------------- 投入の引き渡し（HANDED_OFFは登録完了ではない）

def mk_ledger(tmp_path):
    led = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    man = lambda **k: {"asin_count": 100, "direction": "EXPORT", "business_market": "US", "repeat_of_earlier_cycle": False, "source_id": 1, **k}  # noqa: E731
    led.append("planned", "PLANNED", manifest=man())
    led.append("repeat", "PLANNED", manifest=man(repeat_of_earlier_cycle=True))
    led.append("linked", "PLANNED", manifest=man())
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "55"}), ("MEMO_SAVED", {}), ("LINKED", {"tool_run_id": "55"})):
        led.append("linked", ev, **kw)
    led.append("unknown", "PLANNED", manifest=man())
    led.append("unknown", "SUBMIT_STARTED", before_run_ids=[])
    led.append("unknown", "OUTCOME_UNKNOWN")
    return led


def submit_state(*keys, status="PENDING"):
    st = State("wf-s", "t", {}, "h")
    for k in keys:
        st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", "US", status=status, result={"key": k, "asin_count": 100})
    return st


def test_handoff_is_not_registration_and_never_writes_the_ledger(tmp_path):
    led = mk_ledger(tmp_path)
    before = led.path.read_bytes()
    st = submit_state("planned", "linked", "unknown", "repeat", "nokey")
    ch = handoff_submit_tasks(st, led.all_states(), NOW)
    s = {k: st.tasks[f"submit:{k}"] for k in ("planned", "linked", "unknown", "repeat", "nokey")}
    assert s["planned"].status == "DONE" and s["planned"].result["closed_reason"] == "HANDED_OFF"
    assert s["planned"].result["handoff"]["state"] == "HANDED_OFF" and s["planned"].result["handoff"]["ledger_state"] == "PLANNED"
    assert "登録は未完了" in s["planned"].detail and not s["planned"].result.get("adopted")
    assert led.state("planned").state == "PLANNED"                                       # 台帳は未登録のまま（追跡が続く）
    assert s["linked"].status == "DONE" and s["linked"].result["adopted"] and s["linked"].result["closed_reason"] == "LINKED_IN_LEDGER"
    for k in ("unknown", "repeat", "nokey"):                                              # 人の判断が必要なものは触らない
        assert s[k].status == "PENDING", k
    assert len(ch) == 2 and led.path.read_bytes() == before
    from workflow.store import usage
    assert usage(st)["submit_asins"] == 0                                                 # 引き渡しは「投入したASIN」に数えない


def test_handoff_dry_run_changes_nothing(tmp_path):
    led = mk_ledger(tmp_path)
    st = submit_state("planned")
    assert len(handoff_submit_tasks(st, led.all_states(), NOW, record=False)) == 1 and st.tasks["submit:planned"].status == "PENDING"


# ---------------------------------------------------------------- 新しいworkflowの作成条件

def test_lane_is_blocked_only_by_runnable_incomplete_work():
    def wf(**statuses):
        st = State("w", "t", {}, "h")
        for i, (kind, s) in enumerate(statuses.items()):
            st.tasks[kind] = Task(kind, kind.split(":")[0], "US", status=s)
        return st

    assert lane_is_blocked(wf(**{"acquire:US": "WAITING"}))                    # 人の操作待ちは止める（CAPTCHA・ログイン）
    assert lane_is_blocked(wf(**{"acquire:US": "PENDING"}))                    # いま実行できる未着手は止める
    for s in ("HELD", "FAILED", "UNKNOWN", "DONE"):                            # 再試行時刻待ちは新規作成を止めない
        assert not lane_is_blocked(wf(**{"acquire:US": s})), s
    st = wf(**{"acquire:US": "PENDING"})
    st.tasks["acquire:US"].detail = "discover:US がPENDINGのため待機"
    assert not lane_is_blocked(st)                                             # 依存待ちの未着手は止めない
    assert not lane_is_blocked(wf(**{"submit:k": "PENDING"}))                  # 投入は別プロセスの担当


# ---------------------------------------------------------------- 後方互換

def test_old_state_json_is_read_and_new_fields_are_additions_only(tmp_path):
    old = {"version": 1, "workflow_id": "wf-old", "created_at": "t", "config": {}, "config_sha256": "h", "flags": {}, "allow_duplicates": [], "events": [],
           "tasks": [{"id": "acquire:US", "kind": "acquire", "lane": "US", "status": "HELD", "deps": [], "detail": "d",
                      "result": {"sources": {"1": {"mode": "HELD", "reason": "取得ページの上限（200）に達しています"}}, "market": "US"}, "attempts": 1,
                      "updated_at": "", "retry_after": ""}]}
    st = State.from_json(json.dumps(old))
    assert close_carried(st, prog(tmp_path), NOW)[0]["carried"][0]["kind"] == P.PAUSED_PAGE_CAP
    new = json.loads(st.to_json())
    assert set(new) == set(old)                                             # 状態ファイルの最上位の項目は増やさない
    assert set(new["tasks"][0]) == set(old["tasks"][0])                      # Taskの項目も増やさない（新項目はresultの中だけ）
    State.from_json(st.to_json())


# ---------------------------------------------------------------- 整理（reconcile）: 150本相当の合成データ

def build_synthetic(tmp_path):
    """実データ(2,469本中の未完了150本)の形式を模した合成データ: submit PENDING 136(JP103/US33) / submit HELD 42 / acquire:US HELD 2 /
    JP discover・acquire・expand・cross PENDING 各1。同じ投入鍵が複数のworkflowに現れるものを含む。"""
    store = WorkflowStore(tmp_path / "workflow")
    led = SubmissionLedger(tmp_path / "tool_submission" / "ledger.jsonl")
    man = lambda lane, repeat=False: {"asin_count": 5000, "direction": "IMPORT" if lane == "JP" else "EXPORT", "business_market": lane,  # noqa: E731
                                      "repeat_of_earlier_cycle": repeat, "source_id": 1}
    keys = [f"p{i:03d}" for i in range(136)]
    keys[1] = keys[2] = keys[3] = keys[0]                                       # 同じ鍵が4つのworkflowに現れる
    lanes = ["JP"] * 103 + ["US"] * 33
    for i, (k, lane) in enumerate(zip(keys, lanes)):
        if i < 100:                                                              # LINKED
            if led.state(k).state == "NONE":
                led.append(k, "PLANNED", manifest=man(lane))
                for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": str(1000 + i)}), ("MEMO_SAVED", {}), ("LINKED", {})):
                    led.append(k, ev, **kw)
        elif i < 130:                                                            # 未登録(PLANNED)
            led.append(k, "PLANNED", manifest=man(lane))
        elif i < 134:                                                            # 台帳に鍵なし
            pass
        else:                                                                    # 追跡取り下げ
            led.append(k, "PLANNED", manifest=man(lane))
            led.append(k, "SUBMIT_STARTED", before_run_ids=[])
            led.append(k, "OUTCOME_UNKNOWN")
            led.append(k, "TRACKING_ABANDONED", reason="test")
    n = 0
    for i, (k, lane) in enumerate(zip(keys, lanes)):
        st = State(f"wf-20261101-{n:06d}-aaaa", "2026-11-01T00:00:00Z", {}, "h")
        n += 1
        st.tasks[f"discover:{lane}"] = Task(f"discover:{lane}", "discover", lane, status="DONE")
        st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", lane, status="PENDING", result={"key": k, "asin_count": 5000})
        store.save(st)
    held_keys = [f"h{i:02d}" for i in range(42)]
    for i, k in enumerate(held_keys):
        led.append(k, "PLANNED", manifest=man("JP", repeat=i < 30))
    for j, size in enumerate([4] * 9 + [3] * 2):
        st = State(f"wf-20261102-{j:06d}-bbbb", "2026-11-02T00:00:00Z", {}, "h")
        for k in held_keys[sum(([4] * 9 + [3] * 2)[:j]):][:size]:
            st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", "JP", status="HELD", detail="過去の一巡で投入済み", result={"key": k, "asin_count": 100, "repeat": k in held_keys[:30]})
        store.save(st)
    for j, extra in enumerate(("11", "11")):                                     # 同じ出典(11)がどちらのworkflowにも残る
        st = State(f"wf-20261103-{j:06d}-cccc", "2026-11-03T00:00:00Z", {}, "h")
        st.tasks["acquire:US"] = acquire_task({"11": {"mode": "HELD", "reason": "取得ページの上限（200）に達しています"},
                                               str(20 + j): {"mode": "FETCHED", "run_id": 1}})
        store.save(st)
    st = State("wf-20261104-000000-dddd", "2026-11-04T00:00:00Z", {}, "h")
    for kind in ("discover", "acquire", "expand", "cross"):
        st.tasks[f"{kind}:JP"] = Task(f"{kind}:JP", kind, "JP", status="PENDING")
    store.save(st)
    return store, led


def tree_fingerprint(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_reconcile_dry_run_reproduces_the_150_unfinished_classification_and_writes_nothing(tmp_path):
    store, led = build_synthetic(tmp_path)
    before = tree_fingerprint(tmp_path)
    rep = scan(store, led)
    assert tree_fingerprint(tmp_path) == before                                   # 何も書かない（ファイルの追加・更新なし）
    assert rep["incomplete"] == 150 and sum(len(r["tasks"]) for r in rep["workflows"]) >= 150
    assert rep["by_workflow_class"] == {"A": 100, "B": 33, "C": 2, "D": 14, "E": 1}
    # 重複を除いた集計: 同じ投入鍵(p000)が4つのworkflowに現れ、同じ出典(11)が2つに現れるが1件として数える
    sub = rep["by_key_class"]["submit"]
    assert sub == {"A": 97, "B": 30 + 12, "D": 6 + 30}, sub                      # PENDING 133鍵(重複3を除く)+HELD 42鍵
    assert rep["by_key_class"]["source"] == {"C": 1}
    assert rep["duplicate_occurrences"] >= 4
    assert any(k.startswith("D:REPEAT_OF_EARLIER_CYCLE") for k in rep["by_reason"]) and "D:NO_LEDGER_KEY" in rep["by_reason"]
    assert any(k.startswith("D:LEDGER_ABANDONED") for k in rep["by_reason"])
    text = "\n".join(format_scan(rep))
    assert "dry-run" in text and "重複を除いた対象" in text


def test_reconcile_apply_requires_confirmation_backs_up_and_never_touches_the_ledger_or_human_cases(tmp_path):
    store, led = build_synthetic(tmp_path)
    ledger_bytes = led.path.read_bytes()
    pr = P.SourceProgress(tmp_path / "workflow" / "source_progress.jsonl")
    declined = apply_reconcile(store, led, pr, confirm=lambda q: False)
    assert declined["declined"] and declined["changed"] == [] and not (tmp_path / "workflow" / "source_progress.jsonl").exists()
    before_ids = {w: store.load(w).to_json() for w in store.list_ids()}
    out = apply_reconcile(store, led, pr, confirm=lambda q: True)
    after = scan(store, led)
    assert after["incomplete"] == 150 - 100 - 33 - 2 and after["by_workflow_class"] == {"D": 14, "E": 1}   # D/Eだけが残る
    assert led.path.read_bytes() == ledger_bytes                                  # 台帳は書き換えない
    for c in out["changed"]:
        assert Path(c["backup"]).exists() and json.loads(Path(c["backup"]).read_text(encoding="utf-8")) == json.loads(before_ids[c["workflow"]])
    changed_ids = {c["workflow"] for c in out["changed"]}
    assert len(changed_ids) == 136   # A100 + B33 + C2 + 混在1本(重複候補2鍵はD、台帳PLANNEDの2鍵だけ引き渡し。workflow全体はDのまま)
    assert all(store.load(w).to_json() == before_ids[w] for w in store.list_ids() if w not in changed_ids)   # D/Eは変更なし
    t = store.load("wf-20261103-000000-cccc").tasks["acquire:US"]
    assert t.status == "DONE" and t.result["closed_reason"] == "CARRIED_OVER" and t.result["closed_from"]["status"] == "HELD"
    assert pr.state("US", 11).status == "RESUME_WAITING"
    linked = store.load("wf-20261101-000000-aaaa").tasks[f"submit:p000"]
    assert linked.status == "DONE" and linked.result["closed_reason"] == "LINKED_IN_LEDGER"
    planned = store.load("wf-20261101-000100-aaaa").tasks["submit:p100"]
    assert planned.result["closed_reason"] == "HANDED_OFF" and led.state("p100").state == "PLANNED"


def test_reconcile_apply_refuses_while_a_run_is_active(tmp_path):
    store, led = build_synthetic(tmp_path)
    with store.engine_lock("watch"):
        with pytest.raises(RuntimeError, match="稼働中"):
            apply_reconcile(store, led, P.SourceProgress(tmp_path / "p.jsonl"), confirm=lambda q: True)


def test_reconcile_cli_is_dry_run_by_default_and_apply_needs_a_terminal(tmp_path, capsys):
    from workflow import cli

    store, led = build_synthetic(tmp_path)
    before = tree_fingerprint(tmp_path)
    args = ["reconcile", "--state-dir", str(tmp_path / "workflow"), "--submission-dir", str(tmp_path / "tool_submission")]
    assert cli.main(args) == 0 and tree_fingerprint(tmp_path) == before
    assert "dry-run" in capsys.readouterr().out
    assert cli.main(args + ["--apply"]) == 2 and tree_fingerprint(tmp_path) == before     # 端末でのy/n確認が無ければ実行しない
    assert cli.main(args + ["--apply"], confirm=lambda q: False) == 2 and tree_fingerprint(tmp_path) == before


# ---------------------------------------------------------------- 成果表示の台帳側

def test_ledger_outcome_counts_new_submissions_and_unregistered_without_writing(tmp_path):
    from workflow.outcome import ledger_outcome

    led = mk_ledger(tmp_path)
    before = led.path.read_bytes()
    since, until = datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)
    started = {r["key"] for r in started_in_window(led, since, until)}
    assert started == {"linked", "unknown"}                                       # 計画(PLANNED)や引き渡しは「新規投入」に数えない
    o = ledger_outcome(tmp_path / "sub", since, until)
    assert o["started_in_window"]["EXPORT"] == {"keys": 2, "asins": 200}
    assert o["unregistered_now"]["EXPORT"] == {"keys": 2, "asins": 200}            # planned・repeat
    assert led.path.read_bytes() == before
