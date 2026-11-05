"""reconcile の絞り込み（--created-before / --exclude-key / --verified-only / --report、docs/DEVIATIONS.md #100）の単体テスト。
DB・ブラウザ・実データ不要（合成データ・一時ディレクトリのみ）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tool_submission.ledger import SubmissionLedger
from workflow import cli
from workflow import progress as P
from workflow.quarantine import FILE_NAME, NeedsHuman
from workflow.reconcile import apply_reconcile, plan_reconcile, scan
from workflow.store import State, Task, WorkflowStore

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
CUT = datetime(2026, 11, 4, 22, 48, 6, tzinfo=timezone.utc)
BEFORE, AFTER = "2026-11-04T10:00:00Z", "2026-11-04T23:00:00Z"


def ledger_and_inputs(tmp_path):
    led = SubmissionLedger(tmp_path / "tool_submission" / "ledger.jsonl")
    files = tmp_path / "inputs"
    files.mkdir(parents=True)

    def man(name, asins, repeat=False, run=1):
        f = files / f"{name}.txt"
        f.write_text("\n".join(asins) + "\n", encoding="utf-8")
        return {"asin_count": len(asins), "direction": "EXPORT", "business_market": "US", "repeat_of_earlier_cycle": repeat, "source_id": 7,
                "collector_run_id": run, "input_file": str(f)}

    a = [f"B0A{i:07d}" for i in range(10)]
    led.append("reg1", "PLANNED", manifest=man("reg1", a[:6], run=11))
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "801"}), ("MEMO_SAVED", {}), ("LINKED", {})):
        led.append("reg1", ev, **kw)
    led.append("rep_all", "PLANNED", manifest=man("rep_all", a[:4], repeat=True, run=22))             # 全て登録済み
    led.append("rep_all2", "PLANNED", manifest=man("rep_all2", a[1:3], repeat=True, run=23))          # 全て登録済み（保留指定の対象）
    led.append("rep_part", "PLANNED", manifest=man("rep_part", a, repeat=True, run=24))               # 一部のみ登録済み
    led.append("rep_new", "PLANNED", manifest=man("rep_new", ["B0Z0000001"], repeat=True, run=25))    # どれも未登録
    led.append("plain", "PLANNED", manifest=man("plain", ["B0Y0000001"], run=26))                     # 重複候補でない → B（引き渡し）
    led.append("gone", "PLANNED", manifest=man("gone", ["B0X0000001"], run=27))
    led.append("gone", "TRACKING_ABANDONED", reason="test")                                           # 取り下げ（D: LEDGER_ABANDONED）
    return led


def workflows(tmp_path, spec):
    """spec: [(workflow番号, 作成時刻, 鍵)]。鍵 "ghost" は台帳に無い。"""
    store = WorkflowStore(tmp_path / "workflow")
    for n, created, k in spec:
        st = State(f"wf-20261104-{n:06d}-aaaa", created, {}, "h")
        st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", "US", status="HELD", detail="d", result={"key": k, "asin_count": 5, "repeat": k.startswith("rep")})
        store.save(st)
    return store


SPEC = [(1, BEFORE, "rep_all"), (2, BEFORE, "rep_all2"), (3, BEFORE, "rep_part"), (4, BEFORE, "rep_new"), (5, BEFORE, "gone"), (6, BEFORE, "ghost"),
        (7, BEFORE, "plain"), (8, AFTER, "rep_all"), (9, AFTER, "plain")]


def tree(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and "report" not in p.name}


def run(tmp_path, store, led, **kw):
    nh = NeedsHuman(tmp_path / "workflow" / FILE_NAME, now=lambda: NOW)
    out = apply_reconcile(store, led, P.SourceProgress(tmp_path / "workflow" / "source_progress.jsonl", now=lambda: NOW), now=NOW, confirm=lambda q: True,
                          needs=nh, **kw)
    return out, nh


def status_of(store, n, k):
    return store.load(f"wf-20261104-{n:06d}-aaaa").tasks[f"submit:{k}"]


def test_unspecified_options_behave_as_before(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    out, nh = run(tmp_path, store, led, include_d=True)
    assert len(out["changed"]) == 9                                                # 従来どおり全workflow（作成時刻に関係なく）
    assert sorted({i.key for i in nh.open_items()}) == ["ghost", "gone", "rep_all", "rep_all2", "rep_new", "rep_part"]
    assert status_of(store, 7, "plain").result["closed_reason"] == "HANDED_OFF"


def test_verified_only_quarantines_only_all_already_registered_repeat_keys(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    out, nh = run(tmp_path, store, led, include_d=True, verified_only=True)
    assert sorted({i.key for i in nh.open_items()}) == ["rep_all", "rep_all2"]
    for n, k in ((3, "rep_part"), (4, "rep_new"), (5, "gone"), (6, "ghost")):          # PARTIAL・NONE_REGISTERED・ABANDONED・NO_LEDGER_KEY は取り分けない
        assert status_of(store, n, k).status == "HELD"
    assert status_of(store, 7, "plain").result["closed_reason"] == "HANDED_OFF"       # A/B/C の整理は従来どおり


def test_created_before_excludes_newer_workflows_everywhere(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    out, nh = run(tmp_path, store, led, include_d=True, verified_only=True, created_before=CUT)
    assert status_of(store, 8, "rep_all").status == "HELD" and status_of(store, 9, "plain").status == "HELD"   # 切替後のworkflowには触れない（A/Bも）
    assert status_of(store, 1, "rep_all").status == "DONE" and not any(c["workflow"].endswith("-000008-aaaa") for c in out["changed"])
    assert out["plan"]["out_of_scope_workflows"] == 2
    assert [i.workflows[0]["workflow"] for i in nh.open_items() if i.key == "rep_all"] == ["wf-20261104-000001-aaaa"]


def test_created_before_unreadable_created_at_is_out_of_scope(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, [(1, "not-a-date", "plain"), (2, "2026-11-04T10:00:00", "plain")])    # 時刻帯なしはUTC扱い
    out, _ = run(tmp_path, store, led, created_before=CUT)
    assert [c["workflow"] for c in out["changed"]] == ["wf-20261104-000002-aaaa"]


def test_exclude_key_holds_the_key_and_reports_it(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    out, nh = run(tmp_path, store, led, include_d=True, verified_only=True, exclude_keys=["rep_all2", "plain"])
    assert sorted({i.key for i in nh.open_items()}) == ["rep_all"]
    assert status_of(store, 2, "rep_all2").status == "HELD" and status_of(store, 7, "plain").status == "HELD"   # 取り分けも引き渡しも保留
    assert sorted({x["key"] for x in out["plan"]["held"]}) == ["plain", "rep_all2"]
    assert status_of(store, 7, "plain").result.get("closed_reason") is None


def test_exclude_key_accepts_a_unique_prefix_only_from_8_chars(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, [(1, BEFORE, "rep_all2"), (2, BEFORE, "rep_all")])
    rep = scan(store, led)
    plan = plan_reconcile(rep, led.all_states(), include_d=True, exclude_keys=["rep_all2"])
    assert {x["key"] for x in plan["held"]} == {"rep_all2"} and {x["key"] for x in plan["targets"]} == {"rep_all"}
    assert {x["key"] for x in plan_reconcile(rep, led.all_states(), include_d=True, exclude_keys=["rep_all"])["held"]} == {"rep_all"}    # 短い指定は完全一致のみ


def test_dry_run_plan_matches_apply_and_writes_nothing(tmp_path, capsys):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    before = tree(tmp_path)
    rpt = tmp_path / "out" / "report_plan.json"
    args = ["reconcile", "--state-dir", str(tmp_path / "workflow"), "--submission-dir", str(tmp_path / "tool_submission"), "--include-d", "--verified-only",
            "--created-before", "2026-11-04T22:48:06Z", "--exclude-key", "rep_all2", "--report", str(rpt)]
    assert cli.main(args) == 0 and tree(tmp_path) == before
    text = capsys.readouterr().out
    assert "保留: rep_all2" in text and "対象外のworkflow: 2件" in text and "ALL_ALREADY_REGISTERED" in text
    plan = json.loads(rpt.read_text(encoding="utf-8"))
    assert plan["mode"] == "dry-run" and plan["after"] is None and plan["planned_keys"] == ["plain", "rep_all"] and plan["held_keys"] == ["rep_all2"]
    assert plan["before"]["incomplete"] == 9 and plan["filters"]["created_before"] == "2026-11-04T22:48:06Z"
    out, _ = run(tmp_path, store, led, include_d=True, verified_only=True, created_before=CUT, exclude_keys=["rep_all2"])
    assert {(c["workflow"]) for c in out["changed"]} == {x["workflow"] for x in plan["planned"]}


def test_report_has_before_after_processed_and_held_and_ledger_is_untouched(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    led_bytes = led.path.read_bytes()
    rpt = tmp_path / "report.json"
    run(tmp_path, store, led, include_d=True, verified_only=True, created_before=CUT, exclude_keys=["rep_all2"], report=rpt)
    r = json.loads(rpt.read_text(encoding="utf-8"))
    assert r["mode"] == "apply" and r["declined"] is False
    assert r["before"]["incomplete"] == 9 and r["after"]["incomplete"] == 7                         # rep_all(1つ目)とplain(7)が閉じる。切替後(8,9)は残る
    assert r["processed_keys"] == ["plain", "rep_all"] and r["held_keys"] == ["rep_all2"] and r["out_of_scope_workflows"] == 2
    assert sorted((x["workflow"][-11:], x["key"]) for x in r["processed"]) == [("000001-aaaa", "rep_all"), ("000007-aaaa", "plain")]
    assert led.path.read_bytes() == led_bytes
    assert len(list(store.dir("wf-20261104-000001-aaaa").glob("state.json.reconcile-*.bak"))) == 1


def test_report_written_when_declined_and_nothing_changed(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    rpt = tmp_path / "report_r.json"
    before = tree(tmp_path)
    out = apply_reconcile(store, led, P.SourceProgress(tmp_path / "workflow" / "source_progress.jsonl"), confirm=lambda q: False, report=rpt)
    assert out["declined"] and tree(tmp_path) == before
    r = json.loads(rpt.read_text(encoding="utf-8"))
    assert r["declined"] is True and r["processed"] == [] and r["before"] == r["after"]


def test_running_watch_still_aborts_before_any_report(tmp_path):
    led = ledger_and_inputs(tmp_path)
    store = workflows(tmp_path, SPEC)
    with store.engine_lock("watch"):
        with pytest.raises(RuntimeError):
            apply_reconcile(store, led, P.SourceProgress(tmp_path / "p.jsonl"), confirm=lambda q: True, report=tmp_path / "report_r.json")
    assert not (tmp_path / "report_r.json").exists()


def test_cli_rejects_bad_options(tmp_path, capsys):
    base = ["reconcile", "--state-dir", str(tmp_path / "w"), "--submission-dir", str(tmp_path / "s")]
    assert cli.main(base + ["--created-before", "yesterday"]) == 2
    assert cli.main(base + ["--verified-only"]) == 2                                                # --include-d なしでは意味がないので中止
