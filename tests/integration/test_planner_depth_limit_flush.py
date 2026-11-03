"""5,000件蓄積の計画で、検索結果の深さの限界(DEPTH_LIMIT)で閉じた一巡の端数を放出する条件の結合テスト（docs/DEVIATIONS.md #81）。

#78は「COMPLETEが確定するまで端数を放出しない」としていたが、DEPTH_LIMIT（#80。同じページの繰り返しを取り直して確認した確定の
終了状態、CLOSED_NO_CURSOR）は続きが無いので、その一巡に限り端数も放出する。OPEN・BROKEN・その他のCLOSED_NO_CURSOR（再開位置の消失など）は
従来どおり保留。あわせて、planの「一部のみ」の注記を collector_run.completeness（TRUNCATED/PARTIAL）でも判定する。
専用test-db・疑似ページ。外部接続なし。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from asin_collector.source_runner import RunLimits
from tests.integration.acq_helpers import make_runs  # noqa: F401
from tests.integration.test_acquisition_runner import PageLoadError, results
from tests.integration.test_tool_submission_plan import asin, fetched, registry, runs  # noqa: F401
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import PARTIAL_REASON, build_items, write_plan

pytestmark = pytest.mark.integration

URL = "https://www.amazon.com/s?p=2"


def pool(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:07d}" for i in range(n)]


def depth_limit_run(factory, registry, runs, tmp_path, items, *, tag, units):
    """items(1ページ目)の直後に同じ内容のページが返り、取り直しも同じ → DEPTH_LIMIT（CLOSED_NO_CURSOR）で閉じる一巡。"""
    _, run_id, rep, _, _ = fetched(
        factory, registry, runs, tmp_path, "US", [results(items, URL), results(items, URL), results(items, URL)],
        origin_asin=asin(units), units=units, name=f"{tag}.csv", limits=RunLimits(close_on_repeated_page=True))
    assert (rep.outcome, rep.truncation_reason) == ("SUCCESS_TRUNCATED", "DEPTH_LIMIT")
    return run_id


def row(factory, run_id):
    with factory.get_session("US") as conn, conn.begin():
        return conn.execute(text("SELECT status, outcome, completeness, truncation_reason, cycle_state FROM collector_run WHERE id = :i"),
                            {"i": run_id}).one()


def plan(factory, run_id, ledger, tmp_path, **kw):
    return build_items(factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger, batch_target=5000, **kw)


def test_depth_limit_cycle_releases_the_remainder_below_the_batch_target(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2D", 5003)
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, asins, tag="dl", units=1)
    r = row(app_role_factory, run_id)
    assert (r.cycle_state, r.completeness) == ("CLOSED_NO_CURSOR", "TRUNCATED")
    ledger = SubmissionLedger(tmp_path / "dl.jsonl")
    items, files = plan(app_role_factory, run_id, ledger, tmp_path)
    assert sorted(i.asin_count for i in items) == [3, 5000]  # 端数の3件も放出する
    assert sorted(a for f in files for a in f.decode().split()) == sorted(asins)
    assert all(i.input_is_partial and "一部のASIN" in i.partial_reason for i in items)  # 上限打切り由来の注記は維持
    write_plan(items, files, ledger)
    again, again_files = plan(app_role_factory, run_id, ledger, tmp_path)  # 計画済みの再作成は同じキー・同じ内容（別の投入にならない）
    assert [i.key for i in again] == [i.key for i in items] and again_files == files
    write_plan(again, again_files, ledger)
    assert len(ledger.all_states()) == 2


def test_depth_limit_cycle_with_less_than_one_batch_is_released_whole(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2E", 7)
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, asins, tag="small", units=2)
    items, files = plan(app_role_factory, run_id, SubmissionLedger(tmp_path / "small.jsonl"), tmp_path)
    assert [i.asin_count for i in items] == [7] and files[0].decode().split() == asins


def test_depth_limit_release_is_idempotent_for_the_same_input(app_role_factory, registry, runs, tmp_path):
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, pool("B2F", 12), tag="idem", units=3)
    ledger = SubmissionLedger(tmp_path / "idem.jsonl")
    items, files = plan(app_role_factory, run_id, ledger, tmp_path)
    again, again_files = plan(app_role_factory, run_id, ledger, tmp_path, flush_buffer=True)  # 計画前に何度作っても同じキー・同じ内容
    assert [i.key for i in again] == [i.key for i in items] and again_files == files
    write_plan(items, files, ledger)
    write_plan(again, again_files, ledger)  # 同じ計画の再書込みで重複しない
    assert len(ledger.all_states()) == 1


def test_open_cycle_still_holds_the_remainder(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2G", 5003)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins, URL)],
                                   origin_asin=asin(4), units=4, name="open.csv", limits=RunLimits(max_pages=1))
    assert (rep.outcome, rep.truncation_reason) == ("SUCCESS_TRUNCATED", "PAGE_LIMIT")
    assert row(app_role_factory, run_id).cycle_state == "OPEN"
    items, files = plan(app_role_factory, run_id, SubmissionLedger(tmp_path / "open.jsonl"), tmp_path)
    assert [i.asin_count for i in items] == [5000] and files[0].decode().split() == asins[:5000]


def test_broken_cycle_still_holds_the_remainder_even_when_flush_is_requested(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2H", 5003)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US",
                                   [results(asins, URL), results(asins, URL)], origin_asin=asin(5), units=5, name="broken.csv")
    assert rep.outcome == "PARTIAL" and row(app_role_factory, run_id).cycle_state == "BROKEN"  # 設定falseでは従来どおりREPEATED_PAGEでBROKEN
    ledger = SubmissionLedger(tmp_path / "broken.jsonl")
    for kw in ({}, {"flush_buffer": True}):
        items, files = plan(app_role_factory, run_id, ledger, tmp_path, allow_saved_partial=True, **kw)
        assert [i.asin_count for i in items] == [5000]
    small = pool("B2I", 9)
    _, run2, rep2, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(small, URL), results(small, URL)],
                                  origin_asin=asin(6), units=6, name="broken2.csv")
    assert row(app_role_factory, run2).cycle_state == "BROKEN"
    assert plan(app_role_factory, run2, ledger, tmp_path, allow_saved_partial=True, flush_buffer=True) == ([], [])


def test_failed_cycle_still_holds_the_remainder(app_role_factory, registry, runs, tmp_path):
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US",
                                   [results(pool("B2J", 9), URL), PageLoadError("temporary error")], origin_asin=asin(7), units=7, name="fail.csv")
    assert rep.outcome == "PARTIAL"
    assert plan(app_role_factory, run_id, SubmissionLedger(tmp_path / "fail.jsonl"), tmp_path, allow_saved_partial=True) == ([], [])


def test_other_closed_no_cursor_truncation_reasons_still_hold_the_remainder(app_role_factory, superuser_url, registry, runs, tmp_path):
    """再開位置の消失など、DEPTH_LIMIT以外の CLOSED_NO_CURSOR は末尾到達と見なさない。"""
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, pool("B2K", 9), tag="other", units=8)
    ledger = SubmissionLedger(tmp_path / "other.jsonl")
    assert [i.asin_count for i in plan(app_role_factory, run_id, ledger, tmp_path)[0]] == [9]
    for reason in ("PAGE_LIMIT", "ASIN_LIMIT"):
        set_run(superuser_url, run_id, truncation_reason=reason)
        r = row(app_role_factory, run_id)
        assert (r.cycle_state, r.outcome, r.truncation_reason) == ("CLOSED_NO_CURSOR", "SUCCESS_TRUNCATED", reason)
        assert plan(app_role_factory, run_id, ledger, tmp_path, flush_buffer=True) == ([], [])


def test_complete_cycle_still_releases_the_remainder(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2L", 5003)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins)], origin_asin=asin(9), units=9, name="done.csv")
    assert rep.outcome == "SUCCESS_NONEMPTY" and row(app_role_factory, run_id).cycle_state == "COMPLETE"
    items, files = plan(app_role_factory, run_id, SubmissionLedger(tmp_path / "done.jsonl"), tmp_path)
    assert sorted(i.asin_count for i in items) == [3, 5000]
    assert all(not i.input_is_partial and i.partial_reason is None for i in items)  # 通常の完了は従来どおり注記なし


def test_non_batch_plan_is_unchanged_for_depth_limit(app_role_factory, registry, runs, tmp_path):
    asins = pool("B2M", 6)
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, asins, tag="nb", units=10)
    items, _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert [i.asin_count for i in items] == [6] and items[0].input_is_partial is True


# ---------------------------------------------------------------- 「一部のみ」の注記（completeness も見る）

def set_run(superuser_url, run_id, **cols):
    """完了済みのrunはトリガーで変更できないので、テストデータの用意だけ superuser でトリガーを外して書き換える。"""
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    eng = create_engine(superuser_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text(f"UPDATE collector_run SET {sets} WHERE id = :i"), {**cols, "i": run_id})
    finally:
        eng.dispose()


def test_success_nonempty_with_truncated_completeness_is_marked_partial(app_role_factory, superuser_url, registry, runs, tmp_path):
    asins = pool("B2N", 5)
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins)], origin_asin=asin(11), units=11, name="comp.csv")
    assert rep.outcome == "SUCCESS_NONEMPTY"
    (before,), _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert (before.input_is_partial, before.partial_reason) == (False, None)  # completeness='COMPLETE' は従来どおり注記なし
    set_run(superuser_url, run_id, completeness="TRUNCATED")
    (it,), _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert it.run_outcome == "SUCCESS_NONEMPTY"  # 結果の表示は変えない
    assert it.input_is_partial is True and it.partial_reason == PARTIAL_REASON["SUCCESS_TRUNCATED"]
    set_run(superuser_url, run_id, completeness="PARTIAL")
    (it2,), _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert it2.input_is_partial is True and it2.partial_reason == PARTIAL_REASON["PARTIAL"]
    set_run(superuser_url, run_id, completeness=None)  # 旧取得（completenessなし）も従来どおり
    (it3,), _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert (it3.input_is_partial, it3.partial_reason) == (False, None)


def test_outcome_based_partial_notes_are_unchanged(app_role_factory, superuser_url, registry, runs, tmp_path):
    run_id = depth_limit_run(app_role_factory, registry, runs, tmp_path, pool("B2O", 4), tag="oc", units=12)
    set_run(superuser_url, run_id, completeness=None)  # completenessが無くても outcome=SUCCESS_TRUNCATED で従来どおり「一部のみ」
    (it,), _ = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert it.input_is_partial is True and it.partial_reason == PARTIAL_REASON["SUCCESS_TRUNCATED"]
