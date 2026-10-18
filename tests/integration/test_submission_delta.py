"""投入差分（同じ出典・業務方向）の結合テスト: 同じ一巡で投入済み/予約済み/結果不明のASINは再送しない。過去の別の一巡のASINは
黙って再送も永久除外もせず、新しいASINとは別の入力（repeat）にして明示許可を待つ。別の出典・別の方向は別に判定する。実DB(test-db)・台帳はファイル。"""
from __future__ import annotations

from pathlib import Path

import pytest

from asin_collector.source_runner import RunLimits, run_claimed_source
from tests.integration.test_continuation import Site, fast_cfg, mk_pages, new_site, registry, runs  # noqa: F401
from tool_submission.ledger import SubmissionLedger
from tool_submission.planner import _occupancy, build_items, write_plan

pytestmark = pytest.mark.integration


def make_run(runs, factory, site, source_id, pages):
    claim = runs.claim("US", source_id)
    rep = run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim.run_id, rep


def phantom(ledger: SubmissionLedger, tmp_path, key, *, run_id, source_id, source_key, cycle_key, asins, direction="EXPORT", market="US",
            events=("SUBMIT_STARTED",), file=None):
    """別のrun/出典の投入計画を台帳に直接置く（状態は events で作る）。"""
    f = tmp_path / (file or f"phantom_{key}.csv")
    f.write_text("\n".join(asins) + "\n", encoding="utf-8")
    manifest = {"key": key, "collector_run_id": run_id, "fetch_market": market, "source_id": source_id, "source_key": source_key, "direction": direction,
                "business_market": "US" if direction == "EXPORT" else "JP", "input_file": str(f), "cycle_key": cycle_key, "asin_count": len(asins)}
    ledger.append(key, "PLANNED", manifest=manifest)
    for ev in events:
        name, extra = (ev if isinstance(ev, tuple) else (ev, {}))
        if name == "SUBMIT_STARTED":
            ledger.append(key, "SUBMIT_STARTED", before_run_ids=[1], attempt=1, options={"バリエーション": False})
        else:
            ledger.append(key, name, **extra)
    return manifest


def ids(items):
    return [(i.repeat_of_earlier_cycle, i.asin_count) for i in items]


def test_same_cycle_asins_already_submitted_reserved_or_unknown_are_removed_but_unsent_ones_return(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(4)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 2)
    run2, _ = make_run(runs, app_role_factory, site, src.source_id, 2)
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    with app_role_factory.get_session("US") as conn:
        from sqlalchemy import text
        with conn.begin():
            cyc = conn.execute(text("SELECT cycle_id FROM collector_run WHERE id = :i"), {"i": run2}).scalar_one()
    ck = f"c{cyc}"
    run2_asins = [a for pg in pages[2:] for a in pg]
    shared = run2_asins[:2]
    # 1) 未送信の投入計画に予約済み（PLANNED）→ 別計画へ重複追加しない
    phantom(ledger, tmp_path, "k_reserved", run_id=999001, source_id=src.source_id, source_key="X", cycle_key=ck, asins=shared[:1], events=())
    items, _ = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 5)] and items[0].same_cycle_excluded == 1
    # 2) 送信開始済み(SUBMITTING)・結果不明(UNKNOWN)のASINも再送対象から外す（照合待ち）
    phantom(ledger, tmp_path, "k_unknown", run_id=999002, source_id=src.source_id, source_key="X", cycle_key=ck, asins=shared[1:], events=("SUBMIT_STARTED", "OUTCOME_UNKNOWN"))
    items, files = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 4)] and items[0].same_cycle_excluded == 2
    assert not set(shared) & set(files[0].decode().split())
    # 3) 送信されていないと確認できたもの(NOT_SUBMITTED)は予約から外れ、再び対象になる
    phantom(ledger, tmp_path, "k_notsent", run_id=999003, source_id=src.source_id, source_key="X", cycle_key=ck, asins=[run2_asins[2]], events=("SUBMIT_STARTED", "NOT_SUBMITTED_VERIFIED"))
    items, files = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 4)]
    text_ = files[0].decode()
    assert run2_asins[2] in text_ and not set(shared) & set(text_.split())
    # 同じrun自身の計画は自分自身を除外しない（再計画しても同じキー）
    write_plan(items, files, ledger)
    again, _ = build_items(app_role_factory, "US", run2, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert [i.key for i in again] == [i.key for i in items]
    # run1は別の入力（自分のASINだけ）
    items1, _ = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items1) == [(False, 6)] and items1[0].fetched_from["cycle_key"] == items1[0].cycle_key


def test_asins_from_an_earlier_cycle_are_split_into_a_separate_repeat_input_and_new_asins_are_not_held_back(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(2)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 5)
    asins = [a for pg in pages for a in pg]
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    # 旧方式(cycleなし)のrun82のような過去の投入に、3件が含まれている（過去の別の一巡）
    phantom(ledger, tmp_path, "k_legacy", run_id=82, source_id=src.source_id, source_key="X", cycle_key="legacy:run82", asins=asins[:3], events=("SUBMIT_STARTED",))
    items, files = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert sorted(ids(items)) == [(False, 3), (True, 3)]
    new_item = next(i for i in items if not i.repeat_of_earlier_cycle)
    rep_item = next(i for i in items if i.repeat_of_earlier_cycle)
    assert new_item.key != rep_item.key and new_item.memo == rep_item.memo and rep_item.input_file.endswith(f"_repeat_{rep_item.key}.csv")
    content = {i.key: f.decode().split() for i, f in zip(items, files)}
    assert content[rep_item.key] == asins[:3] and content[new_item.key] == asins[3:]  # 過去に投入済みの分だけがrepeat。新しい分は別の入力
    # 永久に除外はしない: 台帳が変わっても（過去の投入が無ければ）全て新しいASINとして計画できる
    empty = SubmissionLedger(tmp_path / "empty.jsonl")
    items2, _ = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=empty)
    assert ids(items2) == [(False, 6)]


def test_other_sources_and_other_directions_are_judged_separately_and_provenance_is_kept(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(1)
    src, site = new_site(app_role_factory, registry, pages)
    run1, _ = make_run(runs, app_role_factory, site, src.source_id, 5)
    asins = pages[0]
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    # 別の出典(source_id違い)が、同じASINを同じ方向に投入済み → このセラーの入力からは引かない。参照情報(also_from)として残す
    phantom(ledger, tmp_path, "k_other_src", run_id=777, source_id=src.source_id + 1000, source_key="OTHERSELLER", cycle_key="c777", asins=asins[:2], events=("SUBMIT_STARTED",))
    # 同じ出典・別の方向(IMPORT)の投入は、EXPORTの判定に影響しない
    phantom(ledger, tmp_path, "k_other_dir", run_id=778, source_id=src.source_id, source_key="X", cycle_key="c778", asins=asins, direction="IMPORT", events=("SUBMIT_STARTED",))
    items, files = build_items(app_role_factory, "US", run1, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    assert ids(items) == [(False, 3)] and items[0].same_cycle_excluded == 0
    assert files[0].decode().split() == asins  # 別の出典に同じASINがあっても、このセラーの入力に残る（出典情報を失わない）
    (af,) = items[0].also_from
    assert af["source_key"] == "OTHERSELLER" and af["shared_asins"] == 2 and af["basis"] == "ledger_submission_input"  # 取得記録ではなく台帳の入力からの参照情報
    assert items[0].fetched_from["basis"] == "collector_asin" and items[0].fetched_from["collector_run_id"] == run1  # 実際の取得記録は別に持つ


def test_occupancy_is_pure_on_the_ledger_and_ignores_the_same_run(tmp_path):
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    phantom(ledger, tmp_path, "a", run_id=1, source_id=5, source_key="S", cycle_key="c1", asins=["B0A"], events=("SUBMIT_STARTED",))
    phantom(ledger, tmp_path, "b", run_id=2, source_id=5, source_key="S", cycle_key="c2", asins=["B0B"], events=())
    by_cycle, others = _occupancy(ledger.all_states(), run_id=2, source_id=5, direction="EXPORT", market="US")
    assert by_cycle == {"c1": {"B0A"}} and others == []  # 自分のrun(2)は除く
    by_cycle, others = _occupancy(ledger.all_states(), run_id=3, source_id=6, direction="EXPORT", market="US")
    assert by_cycle == {} and {o["source_id"] for o in others} == {5}


# ---------------------------------------------------------------- 中断・失敗runの保存済みASINの引き継ぎ（carried_from_runs、DEVIATIONS #64）

from sqlalchemy import text  # noqa: E402

from acquisition.registry import SourceRegistry  # noqa: E402
from asin_collector.source_runner import AccessBudgetExhausted, load_source, source_url  # noqa: E402
from tests.integration.acq_helpers import expire_run, new_source  # noqa: E402
from tests.integration.test_continuation import Crash  # noqa: E402
from tool_submission.model import SubmissionError  # noqa: E402


class Budget:
    """n回だけページを開ける疑似の日次枠（尽きたらページを開かずにAccessBudgetExhausted）。"""

    def __init__(self, base, n):
        self.base, self.n, self.calls = base, n, 0

    def fetch(self, url):
        if self.calls >= self.n:
            raise AccessBudgetExhausted("日次枠（テスト）")
        self.calls += 1
        return self.base.fetch(url)

    def wait_for_captcha_solved(self):
        return self.base.wait_for_captcha_solved()


def _go(runs, factory, fetcher, source_id, pages, market="US"):
    claim = runs.claim(market, source_id)
    rep = run_claimed_source(runs, factory, fetcher, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim, rep


def _crash_after_saving(runs, factory, site, source_id, page_url, monkeypatch, market="US"):
    """page_url を保存した直後にプロセスが死ぬ → leaseの期限切れで回収（INTERRUPTED/LEASE_EXPIRED）。"""
    claim = runs.claim(market, source_id)
    orig = runs.append_asins

    def die(c, asins, pages=1, *, cursor=None):
        n = orig(c, asins, pages, cursor=cursor)
        if cursor and cursor.last_page_url == page_url:
            raise Crash()
        return n

    monkeypatch.setattr(runs, "append_asins", die)
    with pytest.raises(Crash):
        run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
    monkeypatch.setattr(runs, "append_asins", orig)
    expire_run(factory, market, claim.run_id)
    assert runs.reap_expired(market, source_id) == [claim.run_id]
    return claim


def _inputs(factory, run_id, tmp_path, ledger=None, market="US"):
    items, files = build_items(factory, market, run_id, out_dir=tmp_path / "out", direction_override="EXPORT", ledger=ledger)
    return items, files, [a for f in files for a in f.decode().split()]


def _run_state(factory, run_id, market="US"):
    with factory.get_session(market) as conn, conn.begin():
        return tuple(conn.execute(text("SELECT status, outcome, interrupt_kind, cycle_state FROM collector_run WHERE id = :i"), {"i": run_id}).one())


def test_saved_asins_of_interrupted_and_failed_runs_reach_the_next_usable_run_through_continue_and_finalize_without_gaps(
        app_role_factory, registry, runs, fast_cfg, tmp_path, monkeypatch):
    pages = mk_pages(7)
    src, site = new_site(app_role_factory, registry, pages)
    sid = src.source_id
    c1, r1 = _go(runs, app_role_factory, site, sid, 1)  # 1ページ目（使える取得: 上限打切り）
    assert r1.outcome == "SUCCESS_TRUNCATED"
    c2, r2 = _go(runs, app_role_factory, Budget(site, 2), sid, 9)  # 重なり確認(1) + 2ページ目を保存 → 3ページ目で日次枠切れ
    assert r2.outcome == "DAILY_LIMIT" and _run_state(app_role_factory, c2.run_id) == ("INTERRUPTED", "INTERRUPTED", "DAILY_LIMIT", "OPEN")
    site.captcha_pages = {4}
    c3, r3 = _go(runs, app_role_factory, site, sid, 9)  # 重なり確認(2) + 3ページ目を保存 → 4ページ目でCAPTCHA未解決（失敗・OPEN）
    assert (r3.outcome, r3.error_code) == ("PARTIAL", "CAPTCHA_UNSOLVED")
    site.captcha_pages = set()
    c4 = _crash_after_saving(runs, app_role_factory, site, sid, site.u(4), monkeypatch)  # 4ページ目を保存した直後に死ぬ
    c5, r5 = _go(runs, app_role_factory, site, sid, 1)  # 続き: 5ページ目（使える取得）
    assert (r5.outcome, r5.plan) == ("SUCCESS_TRUNCATED", "CONTINUE")
    c6 = _crash_after_saving(runs, app_role_factory, site, sid, site.u(7), monkeypatch)  # 6・7ページ目（末尾）を保存した直後に死ぬ
    c7, r7 = _go(runs, app_role_factory, site, sid, 9)  # 末尾到達の確定（ページを開かない）
    assert (r7.outcome, r7.plan, r7.pages) == ("SUCCESS_NONEMPTY", "FINALIZE", 0)
    # 使えない取得runは単独では投入計画にできない（引き継ぎ先がある）
    for c in (c2, c3, c4, c6):
        with pytest.raises(SubmissionError):
            _inputs(app_role_factory, c.run_id, tmp_path)
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    sent = []
    for run_id, want_pages, carried in ((c1.run_id, [1], []), (c5.run_id, [2, 3, 4, 5], [c2.run_id, c3.run_id, c4.run_id]),
                                        (c7.run_id, [6, 7], [c6.run_id])):
        items, files, asins = _inputs(app_role_factory, run_id, tmp_path, ledger)
        assert asins == [a for n in want_pages for a in pages[n - 1]]  # 保存順のまま、欠落なく
        assert items[0].carried_from_runs == carried and items[0].fetched_from["carried_from_runs"] == carried
        write_plan(items, files, ledger)  # 投入計画として予約（以降の計画では同じ一巡の予約済みとして扱われる）
        again, _, _ = _inputs(app_role_factory, run_id, tmp_path, ledger)
        assert [i.key for i in again] == [i.key for i in items]  # 再計画しても同じキー（重複した計画を作らない）
        sent += asins
    assert sorted(sent) == sorted(a for pg in pages for a in pg) and len(sent) == len(set(sent)) == 21  # 全期間で1回ずつ


def test_carry_is_limited_to_the_same_market_source_and_cycle(app_role_factory, registry, runs, fast_cfg, tmp_path, monkeypatch):
    # X: 一巡A（2ページ目を保存した後に繰り返しページでBROKEN）→ 一巡B（中断runあり）
    pagesA = mk_pages(4, prefix="B0V")
    pagesA[2] = list(pagesA[1])  # 3ページ目が2ページ目と同じ内容 → REPEATED_PAGE
    x, siteA = new_site(app_role_factory, registry, pagesA)
    _go(runs, app_role_factory, siteA, x.source_id, 1)
    a2, ra2 = _go(runs, app_role_factory, siteA, x.source_id, 9)
    assert ra2.error_code == "REPEATED_PAGE" and len(asins_in(app_role_factory, a2.run_id)) == 3  # 一巡Aの使えないrunにも保存済みASINがある
    pagesB = mk_pages(4, prefix="B0W")
    siteB = Site(pagesB, siteA.base)
    b1, _ = _go(runs, app_role_factory, siteB, x.source_id, 1)  # 一巡B（RESTART_BROKEN）
    b2, rb2 = _go(runs, app_role_factory, Budget(siteB, 2), x.source_id, 9)
    assert rb2.outcome == "DAILY_LIMIT"
    # Y: 別の出典（US）の中断run
    y, siteY = new_site(app_role_factory, registry, mk_pages(3, prefix="B0Y"))
    _go(runs, app_role_factory, siteY, y.source_id, 1)
    yi, ry = _go(runs, app_role_factory, Budget(siteY, 2), y.source_id, 9)
    assert ry.outcome == "DAILY_LIMIT"
    # 同じセラーのJP市場の出典の中断run
    key = load_source(app_role_factory, "US", x.source_id).key_primary
    xj = new_source(registry, "JP", key=key)
    siteJ = Site(mk_pages(3, prefix="B0J"), source_url("JP", load_source(app_role_factory, "JP", xj.source_id)))
    _go(runs, app_role_factory, siteJ, xj.source_id, 1, market="JP")
    ji, rj = _go(runs, app_role_factory, Budget(siteJ, 2), xj.source_id, 9, market="JP")
    assert rj.outcome == "DAILY_LIMIT"
    b3, rb3 = _go(runs, app_role_factory, siteB, x.source_id, 1)
    assert rb3.outcome == "SUCCESS_TRUNCATED"
    items, _, asins = _inputs(app_role_factory, b3.run_id, tmp_path)
    assert items[0].carried_from_runs == [b2.run_id]  # 同じ市場・出典・一巡の中断runだけ
    assert asins == pagesB[1] + pagesB[2]
    others = set(asins_in(app_role_factory, a2.run_id)) | set(asins_in(app_role_factory, yi.run_id)) | set(asins_in(app_role_factory, ji.run_id, "JP"))
    assert others and not others & set(asins)  # 別の一巡・別の出典・別の市場のASINは入らない


def asins_in(factory, run_id, market="US"):
    with factory.get_session(market) as conn, conn.begin():
        return [r[0] for r in conn.execute(text("SELECT asin FROM collector_asin WHERE collector_run_id = :i ORDER BY id"), {"i": run_id})]


def test_carried_asins_already_submitted_reserved_or_unknown_are_never_sent_twice(app_role_factory, registry, runs, fast_cfg, tmp_path):
    pages = mk_pages(4, prefix="B0Z")
    src, site = new_site(app_role_factory, registry, pages)
    c1, _ = _go(runs, app_role_factory, site, src.source_id, 1)
    c2, r2 = _go(runs, app_role_factory, Budget(site, 3), src.source_id, 9)  # 重なり確認 + 2・3ページ目を保存 → 枠切れ
    assert r2.outcome == "DAILY_LIMIT"
    c3, _ = _go(runs, app_role_factory, site, src.source_id, 1)  # 4ページ目（末尾） → 2・3ページ目を引き継ぐ
    with app_role_factory.get_session("US") as conn, conn.begin():
        cyc = conn.execute(text("SELECT cycle_id FROM collector_run WHERE id = :i"), {"i": c3.run_id}).scalar_one()
    ck = f"c{cyc}"
    carried = pages[1] + pages[2]
    ledger = SubmissionLedger(tmp_path / "ledger.jsonl")
    phantom(ledger, tmp_path, "k_linked", run_id=990001, source_id=src.source_id, source_key="X", cycle_key=ck, asins=carried[0:1],
            events=("SUBMIT_STARTED", ("RUN_IDENTIFIED", {"tool_run_id": "7001"}), "MEMO_SAVED", "LINKED"))  # 投入済み
    phantom(ledger, tmp_path, "k_planned", run_id=990002, source_id=src.source_id, source_key="X", cycle_key=ck, asins=carried[1:2],
            events=())  # 予約済み（未送信の投入計画）
    phantom(ledger, tmp_path, "k_unknown", run_id=990003, source_id=src.source_id, source_key="X", cycle_key=ck, asins=carried[2:3],
            events=("SUBMIT_STARTED", "OUTCOME_UNKNOWN"))  # 結果不明
    phantom(ledger, tmp_path, "k_notsent", run_id=990004, source_id=src.source_id, source_key="X", cycle_key=ck, asins=carried[3:4],
            events=("SUBMIT_STARTED", "NOT_SUBMITTED_VERIFIED"))  # 送信されていないと確認済み → 対象に戻る
    phantom(ledger, tmp_path, "k_old", run_id=990005, source_id=src.source_id, source_key="X", cycle_key="legacy:run1", asins=carried[4:5],
            events=("SUBMIT_STARTED", ("RUN_IDENTIFIED", {"tool_run_id": "7002"}), "LINKED"))  # 過去の別の一巡 → repeat入力（明示許可待ち）
    other = new_source(registry, "US")
    phantom(ledger, tmp_path, "k_other", run_id=990006, source_id=other.source_id, source_key="Y", cycle_key="c1", asins=carried[5:6],
            events=("SUBMIT_STARTED",))  # 別の出典 → この出典の入力からは引かない（also_from）
    items, files, _ = _inputs(app_role_factory, c3.run_id, tmp_path, ledger)
    new_items = [i for i, f in zip(items, files) if not i.repeat_of_earlier_cycle]
    new_asins = [a for i, f in zip(items, files) if not i.repeat_of_earlier_cycle for a in f.decode().split()]
    repeat_asins = [a for i, f in zip(items, files) if i.repeat_of_earlier_cycle for a in f.decode().split()]
    assert not set(carried[0:3]) & set(new_asins + repeat_asins)  # 投入済み・予約済み・結果不明は送らない
    assert carried[3] in new_asins and carried[5] in new_asins  # 未送信と確認済み・別の出典のASINは対象
    assert repeat_asins == [carried[4]]  # 過去の一巡で投入済みは別の入力（黙って再送しない）
    assert new_items[0].carried_from_runs == [c2.run_id] and new_items[0].same_cycle_excluded == 3
    assert any(a["source_id"] == other.source_id for a in new_items[0].also_from)
    assert sorted(new_asins + repeat_asins + carried[0:3]) == sorted(carried + pages[3])  # 欠落なし
