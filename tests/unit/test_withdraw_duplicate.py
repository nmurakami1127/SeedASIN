"""`tool_submission.cli withdraw-duplicate`（重複のため未送信の計画を取り下げる。台帳へ追記のみ）の単体テスト。DB・実データ不要。docs/DEVIATIONS.md #99。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from tool_submission.cli import main
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import write_plan
from workflow.dedupe import find_duplicates
from workflow.reconcile import apply_reconcile, scan
from workflow.store import State, Task, WorkflowStore

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
YES, NO = (lambda q: True), (lambda q: False)


def asins(n, prefix="B0D"):
    return [f"{prefix}{i:07d}" for i in range(n)]


def put(tmp_path, name, items, enc="utf-8"):
    f = tmp_path / name
    f.write_bytes("\n".join(items).encode(enc) + b"\n")
    return f


def manifest(tmp_path, name, items, direction="EXPORT", source="sellerA"):
    return {"asin_count": len(items), "direction": direction, "business_market": "US" if direction == "EXPORT" else "JP", "repeat_of_earlier_cycle": True,
            "source_id": 7, "source_key": source, "collector_run_id": 3, "input_file": str(put(tmp_path, f"{name}.txt", items))}


def tool_csv(tmp_path, name, items, enc="utf-8-sig", header="ASIN,Title"):
    return put(tmp_path, name, [header] + [f"{a},商品{a}" for a in items], enc)


def world(tmp_path):
    led = SubmissionLedger(tmp_path / "tool_submission" / "ledger.jsonl")
    a = asins(5)
    led.append("dup", "PLANNED", manifest=manifest(tmp_path, "dup", a))
    led.append("reg", "PLANNED", manifest=manifest(tmp_path, "reg", a + asins(3, "B0E")))
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "7286215"}), ("MEMO_SAVED", {}), ("LINKED", {})):
        led.append("reg", ev, **kw)
    return led, a


def run(led, *args, confirm=YES):
    return main(["withdraw-duplicate", "--ledger", str(led.path), "--key", "dup", "--evidence-run", "7286215", "--reason", "重複", *args], confirm=confirm)


def test_normal_withdrawal_appends_one_event_and_never_changes_existing_lines(tmp_path, capsys):
    led, a = world(tmp_path)
    csv = tool_csv(tmp_path, "out.csv", a + asins(3, "B0E"))
    before = led.path.read_bytes()
    assert run(led, "--verify-csv", str(csv), "--note", "手動確認") == 0
    out = capsys.readouterr().out
    assert "対象: dup" in out and "根拠: 統合ツール実行 7286215" in out and "理由: 重複" in out and "メモ: 重複のため取り下げ（未送信）。" in out
    after = led.path.read_bytes()
    assert after.startswith(before) and after.count(b"\n") == before.count(b"\n") + 1               # 追記のみ・既存行は不変
    st = led.state("dup")
    d = st.duplicate_withdrawn
    assert st.state == "WITHDRAWN" and d["evidence_run_id"] == "7286215" and d["missing_count"] == 0 and d["overlap_count"] == 5 and len(d["evidence_sha"]) == 64
    assert "統合ツール実行 7286215 の結果に全件含まれる" in d["memo"] and "不足0件" in d["memo"] and "履歴・入力ファイルは削除しない" in d["memo"] and "手動確認" in d["memo"]
    assert (tmp_path / "dup.txt").exists()                                                          # 入力ファイルは残る
    assert [e["event"] for e in led.events() if e["key"] == "dup"] == ["PLANNED", "DUPLICATE_WITHDRAWN"]
    assert led.state("reg").state == "LINKED"                                                       # 他のキーは変わらない


@pytest.mark.parametrize("enc", ["utf-8-sig", "cp932", "utf-8"])
def test_verify_csv_accepts_any_supported_encoding(tmp_path, enc):
    led, a = world(tmp_path)
    csv = tool_csv(tmp_path, "out.csv", a + ["B0Z9999999"], enc=enc)                                # 出力に余分なASINがあってもよい（入力が全て含まれればよい）
    assert run(led, "--verify-csv", str(csv)) == 0 and led.state("dup").state == "WITHDRAWN"


def test_verify_csv_with_missing_asins_is_refused_and_writes_nothing(tmp_path, capsys):
    led, a = world(tmp_path)
    csv = tool_csv(tmp_path, "out.csv", a[:3])
    before = led.path.read_bytes()
    assert run(led, "--verify-csv", str(csv)) == 3
    assert "2件" in capsys.readouterr().out and led.path.read_bytes() == before and led.state("dup").state == "PLANNED"
    assert run(led, "--verify-csv", str(tool_csv(tmp_path, "nocol.csv", a, header="商品名,価格"))) == 3      # ASIN列が無い
    assert run(led, "--verify-csv", str(tool_csv(tmp_path, "empty.csv", []))) == 3
    assert led.path.read_bytes() == before


@pytest.mark.parametrize("setup,state", [
    ([("SUBMIT_STARTED", {"before_run_ids": []})], "SUBMITTING"),
    ([("SUBMIT_STARTED", {"before_run_ids": []}), ("OUTCOME_UNKNOWN", {})], "UNKNOWN"),
    ([("SUBMIT_STARTED", {"before_run_ids": []}), ("NOT_SUBMITTED_VERIFIED", {})], "NOT_SUBMITTED"),     # 試行が1回ある
    ([("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "9"}), ("MEMO_SAVED", {}), ("LINKED", {})], "LINKED"),
])
def test_only_unsent_planned_keys_are_accepted(tmp_path, setup, state, capsys):
    led, a = world(tmp_path)
    for ev, kw in setup:
        led.append("dup", ev, **kw)
    before = led.path.read_bytes()
    assert led.state("dup").state == state
    assert run(led) == 2 and "[拒否]" in capsys.readouterr().out and led.path.read_bytes() == before     # 追記しない
    with pytest.raises(ValueError):
        led.withdraw_duplicate("dup", evidence_run_id="1", reason="x", memo="m")                           # API直接でも拒否


def test_unknown_key_and_missing_arguments_are_refused(tmp_path, capsys):
    led, _a = world(tmp_path)
    assert main(["withdraw-duplicate", "--ledger", str(led.path), "--key", "nokey", "--evidence-run", "1", "--reason", "x"], confirm=YES) == 2
    with pytest.raises(ValueError):
        led.withdraw_duplicate("dup", evidence_run_id="", reason="x", memo="m")
    with pytest.raises(ValueError):
        led.withdraw_duplicate("dup", evidence_run_id="1", reason="  ", memo="m")


def test_confirmation_is_required_and_declining_writes_nothing(tmp_path, capsys):
    led, _a = world(tmp_path)
    before = led.path.read_bytes()
    assert run(led, confirm=NO) == 2 and "取り下げませんでした" in capsys.readouterr().out and led.path.read_bytes() == before
    assert run(led, confirm=None) == 2 and "y/n確認" in capsys.readouterr().out and led.path.read_bytes() == before     # 非対話では実行できない
    from tool_submission.cli import build_parser
    assert "--yes" not in build_parser().format_help() and "yes" not in [a.dest for sp in build_parser()._subparsers._group_actions[0].choices.values() for a in sp._actions]


def test_running_twice_appends_once(tmp_path, capsys):
    led, _a = world(tmp_path)
    assert run(led) == 0
    after_first = led.path.read_bytes()
    assert run(led) == 0 and "既に重複のため取り下げ済み" in capsys.readouterr().out.split("取り下げを台帳に追記しました")[-1]
    assert led.path.read_bytes() == after_first
    st, added = led.withdraw_duplicate("dup", evidence_run_id="2", reason="again", memo="m")
    assert added is False and led.path.read_bytes() == after_first and st.duplicate_withdrawn["evidence_run_id"] == "7286215"


def test_direction_and_source_mismatch_are_only_warnings(tmp_path, capsys):
    led, a = world(tmp_path)
    led.append("other", "PLANNED", manifest=manifest(tmp_path, "other", a, direction="IMPORT", source="sellerB"))
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "555"}), ("MEMO_SAVED", {}), ("LINKED", {})):
        led.append("other", ev, **kw)
    assert main(["withdraw-duplicate", "--ledger", str(led.path), "--key", "dup", "--evidence-run", "555", "--reason", "x"], confirm=YES) == 0   # 警告だけで進められる
    out = capsys.readouterr().out
    assert "【警告】方向が一致しません" in out and "【警告】出典が一致しません" in out and led.state("dup").duplicate_withdrawn["warnings"]
    led2, _ = world(tmp_path / "x") if (tmp_path / "x").mkdir() is None else (None, None)
    assert main(["withdraw-duplicate", "--ledger", str(led2.path), "--key", "dup", "--evidence-run", "999", "--reason", "x"], confirm=YES) == 0
    assert "対応する台帳の鍵が見つからず" in capsys.readouterr().out                                    # 実行IDが台帳に無い: 確認できない警告


@dataclass
class FakeItem:
    key: str
    input_file: str
    input_sha256: str
    status: str = "PLANNED"
    asin_count: int = 5
    direction: str = "EXPORT"


def test_withdrawn_duplicate_key_never_revives_but_plain_plan_withdrawn_can(tmp_path):
    import hashlib
    led, a = world(tmp_path)
    assert run(led) == 0
    n = len(led.events())
    led.append("dup", "PLANNED", manifest=manifest(tmp_path, "dup", a))                              # 直接PLANNEDを追記しても復活しない
    assert led.state("dup").state == "WITHDRAWN" and led.state("dup").duplicate_withdrawn
    f = tmp_path / "plan_in.txt"
    f.write_text("\n".join(a) + "\n", encoding="utf-8")
    item = FakeItem("dup", str(f), hashlib.sha256(f.read_bytes()).hexdigest())
    n = len(led.events())
    res = write_plan([item], [b""], led)                                                             # 再計画しても台帳に追記しない
    assert "重複のため取り下げ済み" in res[0][1] and len(led.events()) == n and led.state("dup").state == "WITHDRAWN"
    # 通常の小口再編成(PLAN_WITHDRAWN)は区別でき、再計画で復活する
    led.append("small", "PLANNED", manifest=manifest(tmp_path, "small", a))
    led.append("small", "PLAN_WITHDRAWN", reason="再編成")
    assert led.state("small").state == "WITHDRAWN" and led.state("small").duplicate_withdrawn is None
    led.append("small", "PLANNED", manifest=manifest(tmp_path, "small", a))
    assert led.state("small").state == "PLANNED"


def test_withdrawn_duplicate_is_excluded_from_duplicate_detection_and_registration(tmp_path):
    from workflow.registration import register_saved  # noqa: F401  (対象状態の確認はコードの条件: PLANNED/NOT_SUBMITTED/RUN_IDENTIFIED/MEMO_SAVEDのみ)
    led, a = world(tmp_path)
    assert run(led) == 0
    item = type("I", (), {"key": "other", "direction": "EXPORT", "business_market": "US", "input_file": str(tmp_path / "dup.txt")})()
    assert all(d["key"] != "dup" for d in find_duplicates(led, item))                                # 重複候補の相手にならない
    assert led.state("dup").state not in ("PLANNED", "NOT_SUBMITTED", "RUN_IDENTIFIED", "MEMO_SAVED")  # bulk registrationの対象外


def test_workflow_reconcile_closes_it_as_class_a_while_plain_withdrawn_stays_human(tmp_path):
    led, a = world(tmp_path)
    led.append("plain", "PLANNED", manifest=manifest(tmp_path, "plain", a))
    led.append("plain", "PLAN_WITHDRAWN", reason="再編成")
    assert run(led) == 0
    store = WorkflowStore(tmp_path / "workflow")
    for i, k in enumerate(("dup", "plain")):
        st = State(f"wf-2026110{i}-000000-aaaa", "2026-11-01T00:00:00Z", {}, "h")
        st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", "US", status="HELD", detail="過去の一巡で投入済み", result={"key": k, "asin_count": 5, "repeat": True})
        store.save(st)
    rep = scan(store, led)
    assert rep["by_key_class"]["submit"] == {"A": 1, "D": 1}                                         # 重複取り下げはA（閉じてよい）、通常のWITHDRAWNはDのまま
    assert rep["d_breakdown"] == {"LEDGER_WITHDRAWN": {"unique_keys": 1, "workflows": 1, "quarantinable": True}} and "A:DUPLICATE_WITHDRAWN" in rep["by_reason"]
    before = led.path.read_bytes()
    out = apply_reconcile(store, led, __import__("workflow.progress", fromlist=["x"]).SourceProgress(tmp_path / "p.jsonl"), now=NOW, confirm=lambda q: True)
    assert led.path.read_bytes() == before                                                           # 台帳は読むだけ
    t = store.load("wf-20261100-000000-aaaa").tasks["submit:dup"]
    assert t.status == "SKIPPED" and t.result["closed_reason"] == "DUPLICATE_WITHDRAWN" and "再送しません" in t.detail
    assert store.load("wf-20261101-000000-aaaa").tasks["submit:plain"].status == "HELD"              # Dは触らない
    assert len(out["changed"]) == 1
    assert scan(store, led)["by_key_class"]["submit"] == {"D": 1}
