"""5,000件を超えるASINの分割登録と端数の放出（docs/DEVIATIONS.md #104）の単体テスト。DB不要。

確認すること: 5,001→5000+1 / 12,300→5000+5000+2300 / 20,000→5000×4（batch_targetの有無・端数放出の有無の全組み合わせ）／
分割の合計＝元の集合・重複なし／周期がOPENのときだけ持ち越し（件数と理由を返す）／区分の恒等式（周期＝計画済み＋除外＋取り下げ＋未計画）／
旧マニフェストの計画を、新項目(split_parent)だけでは更新しない。
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from tool_submission import coverage_audit
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import (
    SubmissionItem, asin_file_bytes, select_for_submission, split_asins, split_parent_id, submission_key,
)
from tool_submission.planner import _manifest_changed, summarize_carryover, write_plan


def pool(n: int, prefix: str = "B0") -> list[str]:
    return [f"{prefix}{i:08d}" for i in range(n)]


CASES = [(5001, [5000, 1]), (12300, [5000, 5000, 2300]), (20000, [5000] * 4), (5000, [5000]), (4999, [4999]), (1, [1])]


@pytest.mark.parametrize("n,expected", CASES)
@pytest.mark.parametrize("batch_target", [0, 5000])
@pytest.mark.parametrize("release", [True, False])
def test_split_sizes_and_totals(n, expected, batch_target, release):
    asins = pool(n)
    planned, held = select_for_submission(asins, batch_target=batch_target, release_remainder=release)
    chunks = split_asins(planned, 5000)
    flat = [a for c in chunks for a in c]
    assert flat + held == asins  # 保存順のまま、投入＋持ち越し＝元の集合（欠けない・重複しない）
    assert len(set(flat)) == len(flat) and not set(flat) & set(held)
    assert all(len(c) <= 5000 for c in chunks)
    if batch_target == 0 or release:
        assert held == [] and [len(c) for c in chunks] == expected  # 端数も含めて全件を5,000件以下に分割
    else:
        full = n // 5000 * 5000  # 周期が未終了(OPEN)のときだけ、満杯でない端数を持ち越す
        assert len(flat) == full and len(held) == n - full
        assert [len(c) for c in chunks if c] == expected[:full // 5000]


def test_smaller_batch_target_still_splits_at_the_input_limit():
    planned, held = select_for_submission(pool(12000), batch_target=2000, release_remainder=False)
    assert (len(planned), len(held)) == (12000, 0)
    assert [len(c) for c in split_asins(planned, 5000)] == [5000, 5000, 2000]
    planned, held = select_for_submission(pool(12300), batch_target=2000, release_remainder=False)
    assert (len(planned), len(held)) == (12000, 300)


@pytest.mark.parametrize("n", [5001, 12300, 20000])
def test_each_chunk_gets_a_unique_key_and_content_hash(n):
    chunks = split_asins(pool(n), 5000)
    shas = [hashlib.sha256(asin_file_bytes(c)).hexdigest() for c in chunks]
    keys = [submission_key(7, "EXPORT", s) for s in shas]
    assert len(set(shas)) == len(set(keys)) == len(chunks)
    assert submission_key(7, "IMPORT", shas[0]) != keys[0]  # 方向が違えば別の鍵（IMPORTとEXPORTを混ぜない）


def test_split_parent_id_is_stable_for_the_same_parent_and_differs_otherwise():
    a = split_parent_id(5, "EXPORT", False, "B000000001")
    assert a == split_parent_id(5, "EXPORT", False, "B000000001")
    assert len({a, split_parent_id(6, "EXPORT", False, "B000000001"), split_parent_id(5, "IMPORT", False, "B000000001"),
                split_parent_id(5, "EXPORT", True, "B000000001"), split_parent_id(5, "EXPORT", False, "B000000002")}) == 5


def test_summarize_carryover_reports_count_and_reason():
    assert summarize_carryover([]) == (0, "")
    total, msg = summarize_carryover([
        {"reason": "OPEN", "asin_count": 2300}, {"reason": "OPEN", "asin_count": 1}, {"reason": "FAILED_RUN_NOT_ALLOWED", "asin_count": 7}])
    assert total == 2308 and msg.startswith("持ち越し2,308件（") and "OPEN" in msg and "2,301件" in msg and "7件" in msg


# ---------------------------------------------------------------- 区分の恒等式

def test_classify_partitions_the_cycle_without_overlap():
    cyc = set(pool(100))
    plans = [{"kind": "planned", "asins": set(pool(40))},
             {"kind": "repeat", "asins": set(pool(50)[40:45])},
             {"kind": "dup_withdrawn", "asins": set(pool(60)[50:55])},
             {"kind": "withdrawn", "asins": set(pool(70)[55:65]) | set(pool(5))}]  # pool(5)は計画済みと重なる（計画済みが優先）
    earlier = set(pool(80)[65:70])
    part = coverage_audit.classify(cyc, plans, earlier)
    assert {k: len(v) for k, v in part.items()} == {"planned": 40, "dup_earlier": 10, "dup_withdrawn": 5, "withdrawn": 10, "unplanned": 35}
    assert sum(len(v) for v in part.values()) == len(cyc) and set().union(*part.values()) == cyc


def test_unplanned_reason():
    r = coverage_audit.unplanned_reason
    assert r("OPEN", True) == "OPEN_CARRYOVER" and r("BROKEN", True) == "BROKEN_NOT_RELEASED"
    assert r("COMPLETE", True) == "TERMINAL_NOT_PLANNED" and r("OPEN", False) == "NO_DIRECTION"


# ---------------------------------------------------------------- 台帳・マニフェスト

def _item(tmp: Path, asins: list[str], *, split_parent=None):
    content = asin_file_bytes(asins)
    sha = hashlib.sha256(content).hexdigest()
    key = submission_key(1, "EXPORT", sha)
    item = SubmissionItem(
        key=key, collector_run_id=1, fetch_market="US", source_id=1, source_type="SELLER", source_key="K", direction="EXPORT",
        business_market="US", memo="輸出_K", run_outcome="SUCCESS_NONEMPTY", input_is_partial=False, partial_reason=None,
        run_completed_at="2026-11-01 00:00:00 UTC", run_pages=1, asin_count=len(asins), total_rows=len(asins), duplicate_rows_removed=0,
        input_file=str(tmp / f"{key}.csv"), input_sha256=sha, origin=None, cycle_key="c1", split_parent=split_parent)
    return item, content


def test_old_manifest_without_split_parent_is_not_rewritten(tmp_path):
    item, content = _item(tmp_path, pool(3), split_parent={"id": "x", "run_id": 1})
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    old = {k: v for k, v in dataclasses.asdict(item).items() if k != "split_parent"}
    old["status"] = "READY"
    ledger.append(item.key, "PLANNED", manifest=old)
    assert not _manifest_changed(old, {**old, "split_parent": {"id": "x"}})  # 新項目だけの違いは変更とみなさない
    assert _manifest_changed(old, {**old, "asin_count": 99, "split_parent": {"id": "x"}})
    ((_, how),) = write_plan([item], [content], ledger)
    assert how == "変更なし" and len(ledger.events()) == 1  # 台帳に追記しない


def test_write_plan_is_idempotent_for_split_inputs(tmp_path):
    ledger = SubmissionLedger(tmp_path / "l.jsonl")
    chunks = split_asins(pool(12300), 5000)
    pairs = [_item(tmp_path, c, split_parent={"id": "p", "chunk_count": 3}) for c in chunks]
    items, files = [p[0] for p in pairs], [p[1] for p in pairs]
    write_plan(items, files, ledger)
    write_plan(items, files, ledger)
    st = ledger.all_states()
    assert len(st) == 3 and {s.state for s in st.values()} == {"PLANNED"} and len(ledger.events()) == 3
    assert sorted(s.manifest["asin_count"] for s in st.values()) == [2300, 5000, 5000]
    assert all(s.manifest["split_parent"]["id"] == "p" for s in st.values())


# ---------------------------------------------------------------- workflow: 持ち越しを結果・ログに出す

def test_expand_reports_the_carryover_count_and_reason(monkeypatch):
    from tests.unit.test_workflow_extra_origin import _engine_for_expand, _expand, _item, cfg_with
    import workflow.engine as E

    cfg = cfg_with(US={"origin_import": 1})
    eng, _ = _engine_for_expand(monkeypatch, cfg, [_item("k-exp", "EXPORT", [1])])

    def fake_build(*a, carryover=None, **k):
        carryover.append({"kind": "HELD_REMAINDER", "reason": "OPEN", "asin_count": 2300, "run_id": 100, "cycle_state": "OPEN"})
        return [_item("k-exp", "EXPORT", [1])], [b"x"]

    monkeypatch.setattr(E, "build_items", fake_build)
    t = _expand(eng, "acquire")
    assert t.result["carryover_asins"] == 2300 and t.result["carryover"][0]["reason"] == "OPEN"
    assert "持ち越し2,300件（" in t.detail and "OPEN" in t.detail


# ---------------------------------------------------------------- audit-coverage: Windows・Linuxのパス

@pytest.mark.parametrize("raw", [
    "exports\\tool_submission\\inputs\\a.csv",                       # Windowsの相対（バックスラッシュ）
    "exports/tool_submission/inputs/a.csv",                          # Linux/スラッシュの相対
    "C:\\Users\\someone\\SeedASIN\\exports\\tool_submission\\inputs\\a.csv",   # 別環境（Windows）の絶対パス
    "inputs\\a.csv",                                                 # 台帳のフォルダー基準の相対
])
def test_resolve_input_path_reads_windows_and_linux_paths(tmp_path, raw):
    base = tmp_path / "repo" / "exports" / "tool_submission"
    (base / "inputs").mkdir(parents=True)
    f = base / "inputs" / "a.csv"
    f.write_text("\ufeffB000000001\nB000000002\n", encoding="utf-8")
    got = coverage_audit.resolve_input_path(raw, [base, base.parent, tmp_path / "repo"])
    assert got is not None and got.samefile(f)
    cache: dict = {}
    assert coverage_audit._read_input(raw, [base, base.parent, tmp_path / "repo"], cache) == ["B000000001", "B000000002"]  # BOM付きも読める
    assert coverage_audit.resolve_input_path("missing\\x.csv", [base]) is None
    assert coverage_audit.resolve_input_path("", [base]) is None
