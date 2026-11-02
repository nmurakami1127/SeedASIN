"""`close_on_repeated_page`（繰り返しページを「限界」として閉じる）の結合テスト。疑似ページ・使い捨てtest-db。外部接続なし。

背景: 大きいセラーでは検索結果の深さに限界があり、その先は同じページが返ると考えられる（実ページでは未確認）。従来は
REPEATED_PAGE で一巡をBROKENにし、先頭から同じASINを取り直していた。設定が true のときだけ、同じURLを1回取り直して
確認し、同じなら SUCCESS_TRUNCATED / DEPTH_LIMIT / 再開位置なし（CLOSED_NO_CURSOR）で閉じる。false の間は従来どおり。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from acquisition.lease import NotEligible
from asin_collector.source_runner import AccessBudgetExhausted, FetchedPage, PageLoadError, RunLimits, run_claimed_source
from tests.integration.test_continuation import (  # noqa: F401
    Site, asins_of, card, decision, fast_cfg, mk_pages, new_site, registry, row, runs,
)
from tool_submission.planner import USABLE_OUTCOMES, build_items

pytestmark = pytest.mark.integration


class ScriptedSite(Site):
    """Siteに、ページ番号ごとの「次の取得だけ差し替える応答」の待ち行列を足す（一時的な不具合の再現）。"""

    def __init__(self, pages, base) -> None:
        super().__init__(pages, base)
        self.once: dict[int, list] = {}

    def fetch(self, url):
        n = self.num(url)
        q = self.once.get(n)
        if q:
            self.urls.append(url)
            item = q.pop(0)
            if isinstance(item, BaseException):
                raise item
            return FetchedPage(item, url, 200)
        return super().fetch(url)


class Budget:
    """n回までしか取得させず、超えたら日次枠切れ（AccessBudgetExhausted）にする。"""

    def __init__(self, base, n: int) -> None:
        self.base, self.left = base, n

    def fetch(self, url):
        if self.left <= 0:
            raise AccessBudgetExhausted("日次枠切れ")
        self.left -= 1
        return self.base.fetch(url)

    def wait_for_captcha_solved(self):
        return self.base.wait_for_captcha_solved()


def new_scripted(factory, registry, pages):
    src, site = new_site(factory, registry, pages)
    s = ScriptedSite(pages, site.base)
    return src, s


def go(runs, factory, site, source_id, *, pages=9, close=True, sleep=lambda s: None):
    claim = runs.claim("US", source_id)
    rep = run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages, close_on_repeated_page=close), sleep=sleep)
    return claim, rep


def full_row(factory, run_id):
    with factory.get_session("US") as conn, conn.begin():
        return conn.execute(text(
            "SELECT status, outcome, completeness, truncation_reason, error_code, error_message, pages_fetched, asin_count, cycle_state, "
            "next_url, next_page_no, last_page_url, interrupt_kind FROM collector_run WHERE id = :i"), {"i": run_id}).one()


def depth_limited_site(factory, registry, depth=3, total=8, prefix="B0G"):
    """depth ページ目までは別内容。depth+1 ページ目以降は depth ページ目と同じ内容が返り続ける（次へのリンクもある）。"""
    pages = mk_pages(total, prefix=prefix)
    src, site = new_scripted(factory, registry, pages)
    for n in range(depth + 1, total + 1):
        site.override[n] = site.html(pages[depth - 1], n)
    return src, site, pages


# ---------------------------------------------------------------- ① 一時的な繰り返しは続けて取得できる

def test_transient_repeat_is_rechecked_once_and_the_crawl_continues(app_role_factory, registry, runs, fast_cfg):
    pages = mk_pages(4)
    src, site = new_scripted(app_role_factory, registry, pages)
    site.once[3] = [site.html(pages[1], 3)]  # 3ページ目の最初の応答だけ2ページ目と同じ内容（一時的な不具合）。取り直すと正しい内容
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.pages, rep.asins, rep.repeat_check_pages) == ("SUCCESS_NONEMPTY", 4, 12, 1)
    assert rep.error_code is None and rep.truncation_reason is None
    assert site.urls == [site.u(1), site.u(2), site.u(3), site.u(3), site.u(4)]  # 取り直しは同じURLを1回だけ
    assert asins_of(app_role_factory, claim.run_id) == [a for p in pages for a in p]
    r = full_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.cycle_state, r.next_url) == ("COMPLETED", "SUCCESS_NONEMPTY", "COMPLETE", None)


def test_transient_repeat_then_a_changed_but_empty_page_is_a_broken_cycle_not_a_limit(app_role_factory, registry, runs, fast_cfg):
    pages = mk_pages(4)
    src, site = new_scripted(app_role_factory, registry, pages)
    site.once[3] = [site.html(pages[1], 3), "<html><body>想定外</body></html>"]
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code, rep.repeat_check_pages) == ("PARTIAL", "EMPTY_PAGE_AFTER_RESULTS", 1)
    assert full_row(app_role_factory, claim.run_id).cycle_state == "BROKEN"


# ---------------------------------------------------------------- ② 確定したら DEPTH_LIMIT で閉じる

def test_confirmed_repeat_closes_as_depth_limit_keeps_saved_asins_and_has_no_resume_position(app_role_factory, registry, runs, fast_cfg):
    src, site, pages = depth_limited_site(app_role_factory, registry)
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.pages, rep.asins, rep.repeat_check_pages) == ("SUCCESS_TRUNCATED", 3, 9, 1)
    assert rep.truncation_reason == "DEPTH_LIMIT" and rep.error_code is None
    assert site.urls == [site.u(1), site.u(2), site.u(3), site.u(4), site.u(4)]  # 4ページ目（繰り返し）を1回だけ取り直す。5ページ目以降へは進まない
    assert asins_of(app_role_factory, claim.run_id) == [a for p in pages[:3] for a in p]  # 保存済みはそのまま残る。繰り返しのページは保存しない
    r = full_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.completeness, r.truncation_reason) == ("COMPLETED", "SUCCESS_TRUNCATED", "TRUNCATED", "DEPTH_LIMIT")
    assert (r.cycle_state, r.next_url, r.next_page_no, r.error_code) == ("CLOSED_NO_CURSOR", None, None, None)
    assert (r.pages_fetched, r.asin_count) == (3, 9)
    # 次の判定: 成功扱い（truncated_counts_as_success の既定どおり）。BROKENではないので RESTART_BROKEN にならず、同じASINを取り直さない
    d = decision(app_role_factory, src.source_id)
    assert d.state == "TOO_RECENT" and d.last_success_at is not None
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)


def test_after_depth_limit_the_next_eligible_run_is_a_fresh_cycle_never_restart_broken(app_role_factory, registry, runs, fast_cfg):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("UPDATE acquisition_interval_config SET truncated_counts_as_success = false WHERE marketplace = 'US'"))
    try:
        src, site, _ = depth_limited_site(app_role_factory, registry, prefix="B0H")
        c1, _ = go(runs, app_role_factory, site, src.source_id)
        d = decision(app_role_factory, src.source_id)
        assert d.eligible and d.plan == "FRESH" and d.plan != "RESTART_BROKEN"  # 設定で成功に数えない場合も、取り直し(BROKEN)扱いではない
        c2 = runs.claim("US", src.source_id)
        r2 = row(app_role_factory, c2.run_id)
        assert (r2.cycle_start_reason, r2.supersedes_cycle_id, r2.restart_streak) == ("FRESH", None, 0)
        assert r2.cycle_id != c1.run_id
    finally:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("UPDATE acquisition_interval_config SET truncated_counts_as_success = true WHERE marketplace = 'US'"))


def test_depth_limit_found_at_the_first_page_of_a_continuation_closes_without_a_resume_position(app_role_factory, registry, runs, fast_cfg):
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0I")
    c1, r1 = go(runs, app_role_factory, site, src.source_id, pages=3)  # 上限(PAGE_LIMIT)で打ち切り。3ページ目まで保存・次は4ページ目
    assert (r1.outcome, r1.truncation_reason) == ("SUCCESS_TRUNCATED", "PAGE_LIMIT") and r1.pages == 3
    assert full_row(app_role_factory, c1.run_id).cycle_state == "OPEN"
    site.urls.clear()
    c2, r2 = go(runs, app_role_factory, site, src.source_id, pages=9)
    assert (r2.plan, r2.outcome, r2.pages, r2.overlap_pages, r2.repeat_check_pages) == ("CONTINUE", "SUCCESS_TRUNCATED", 0, 1, 1)
    assert r2.truncation_reason == "DEPTH_LIMIT"
    assert site.urls == [site.u(3), site.u(4), site.u(4)]  # 重なり確認(3) → 4 → 取り直し(4)
    r = full_row(app_role_factory, c2.run_id)
    assert (r.status, r.outcome, r.cycle_state, r.next_url, r.next_page_no, r.truncation_reason) == \
        ("COMPLETED", "SUCCESS_TRUNCATED", "CLOSED_NO_CURSOR", None, None, "DEPTH_LIMIT")
    assert decision(app_role_factory, src.source_id).state == "TOO_RECENT"


def test_depth_limit_run_is_usable_as_a_submission_input(app_role_factory, registry, runs, fast_cfg, tmp_path):
    assert "SUCCESS_TRUNCATED" in USABLE_OUTCOMES
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0J")
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert rep.truncation_reason == "DEPTH_LIMIT"
    items, files = build_items(app_role_factory, "US", claim.run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert [i.run_outcome for i in items] == ["SUCCESS_TRUNCATED"] and items[0].input_is_partial is True
    assert sorted(a for f in files for a in f.decode().split()) == sorted(a for p in pages[:3] for a in p)


# ---------------------------------------------------------------- ③ false（既定）は従来どおり

def test_default_is_off_and_a_repeated_page_breaks_the_cycle_as_before(app_role_factory, registry, runs, fast_cfg):
    assert RunLimits().close_on_repeated_page is False
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0K")
    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)  # 設定を渡さない=既定
    assert (rep.outcome, rep.error_code, rep.pages, rep.repeat_check_pages, rep.truncation_reason) == ("PARTIAL", "REPEATED_PAGE", 3, 0, None)
    assert site.urls == [site.u(1), site.u(2), site.u(3), site.u(4)]  # 取り直さない
    r = full_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.cycle_state, r.error_code) == ("FAILED", "PARTIAL", "BROKEN", "REPEATED_PAGE")
    d = decision(app_role_factory, src.source_id)
    assert d.plan == "RESTART_BROKEN"  # 従来どおり先頭から取り直し


def test_explicit_false_is_the_same_as_the_default(app_role_factory, registry, runs, fast_cfg):
    src, site, _ = depth_limited_site(app_role_factory, registry, prefix="B0L")
    claim, rep = go(runs, app_role_factory, site, src.source_id, close=False)
    assert (rep.outcome, rep.error_code, rep.repeat_check_pages) == ("PARTIAL", "REPEATED_PAGE", 0)
    assert full_row(app_role_factory, claim.run_id).cycle_state == "BROKEN"


# ---------------------------------------------------------------- ④ 日次枠が尽きた場合・取り直し中の失敗

def test_daily_budget_exhausted_on_the_recheck_is_a_daily_limit_not_a_depth_limit(app_role_factory, registry, runs, fast_cfg):
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0M")
    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, Budget(site, 4), claim, limits=RunLimits(max_pages=9, close_on_repeated_page=True),
                             sleep=lambda s: None)  # 1,2,3,4ページ目までで枠切れ。4ページ目の取り直しは開かない
    assert (rep.outcome, rep.pages, rep.repeat_check_pages) == ("DAILY_LIMIT", 3, 0)  # 取り直しを開いていないので 0
    r = full_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.interrupt_kind, r.cycle_state, r.truncation_reason) == ("INTERRUPTED", "INTERRUPTED", "DAILY_LIMIT", "OPEN", None)
    d = decision(app_role_factory, src.source_id)
    assert d.plan == "CONTINUE"  # 失敗に数えず、保存済みの位置（4ページ目）から翌日に続ける
    # 翌日: 枠があれば、続きの冒頭で繰り返しを確認して閉じる
    site.urls.clear()
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert (r2.outcome, r2.truncation_reason, r2.repeat_check_pages) == ("SUCCESS_TRUNCATED", "DEPTH_LIMIT", 1)
    assert full_row(app_role_factory, c2.run_id).cycle_state == "CLOSED_NO_CURSOR"


def test_the_recheck_counts_as_one_more_page_access_through_the_fetcher(app_role_factory, registry, runs, fast_cfg):
    src, site, _ = depth_limited_site(app_role_factory, registry, prefix="B0N")
    calls = []

    class Counting:
        def fetch(self, url):
            calls.append(url)
            return site.fetch(url)

        def wait_for_captcha_solved(self):
            return site.wait_for_captcha_solved()

    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, Counting(), claim, limits=RunLimits(max_pages=9, close_on_repeated_page=True), sleep=lambda s: None)
    assert len(calls) == rep.pages + rep.overlap_pages + rep.repeat_check_pages + 1 == 5  # 保存3 + 繰り返しの検知1 + 取り直し1
    # 日次枠に数えるアクセス数 = pages + overlap_pages + repeat_check_pages + 検知した1ページ（全て fetcher を通る）


def test_server_error_on_the_recheck_is_a_retryable_failure_not_a_depth_limit_and_not_broken(app_role_factory, registry, runs, fast_cfg):
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0O")
    site.once[4] = [site.html(pages[2], 4), '<html><body><div id="cs_503_logo"></div></body></html>',
                    '<html><body><div id="cs_503_logo"></div></body></html>', '<html><body><div id="cs_503_logo"></div></body></html>']
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code) == ("PARTIAL", "SERVER_ERROR")
    r = full_row(app_role_factory, claim.run_id)
    assert (r.status, r.cycle_state, r.truncation_reason) == ("FAILED", "OPEN", None)  # 一時的な失敗: 位置を保存したまま続けられる


def test_captcha_on_the_recheck_waits_for_a_human_like_any_page(app_role_factory, registry, runs, fast_cfg):
    src, site, pages = depth_limited_site(app_role_factory, registry, prefix="B0P")
    solved = []
    site.wait_for_captcha_solved = lambda: solved.append(1)  # 人が解いた
    captcha = "<html><body>Enter the characters you see below</body></html>"
    site.once[4] = [site.html(pages[2], 4), captcha]  # 検知した1回目は繰り返し、取り直しの最初はCAPTCHA、解消後は再取得(繰り返しのまま)
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert solved == [1]
    assert (rep.outcome, rep.truncation_reason, rep.repeat_check_pages) == ("SUCCESS_TRUNCATED", "DEPTH_LIMIT", 1)


# ---------------------------------------------------------------- ⑤ 他の BROKEN は設定がtrueでも従来どおり

def test_overlap_mismatch_revisited_url_and_empty_page_are_still_broken_when_the_setting_is_on(app_role_factory, registry, runs, fast_cfg):
    # OVERLAP_MISMATCH: 続きの前に取り直した最後のページが、保存した内容と食い違う
    src, site = new_scripted(app_role_factory, registry, mk_pages(4, prefix="B0Q"))
    go(runs, app_role_factory, site, src.source_id, pages=2)
    site.override[2] = site.html(["B0ZZ000001", "B0ZZ000002", "B0ZZ000003"], 2)
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert (r2.outcome, r2.error_code, r2.repeat_check_pages) == ("PARTIAL", "OVERLAP_MISMATCH", 0)
    assert full_row(app_role_factory, c2.run_id).cycle_state == "BROKEN"
    # REVISITED_URL（次へが今のURLを指す）
    s2, loop = new_scripted(app_role_factory, registry, mk_pages(3, prefix="B0R"))
    loop.override[2] = f'<html><body>{card(loop.pages[1])}<a class="s-pagination-next" href="{loop.u(1)}">Next</a></body></html>'
    c3, r3 = go(runs, app_role_factory, loop, s2.source_id)
    assert (r3.error_code, r3.repeat_check_pages) == ("REVISITED_URL", 0) and full_row(app_role_factory, c3.run_id).cycle_state == "BROKEN"
    # EMPTY_PAGE_AFTER_RESULTS
    s3, emp = new_scripted(app_role_factory, registry, mk_pages(3, prefix="B0S"))
    emp.override[2] = "<html><body>想定外</body></html>"
    c4, r4 = go(runs, app_role_factory, emp, s3.source_id)
    assert (r4.error_code, r4.repeat_check_pages) == ("EMPTY_PAGE_AFTER_RESULTS", 0) and full_row(app_role_factory, c4.run_id).cycle_state == "BROKEN"


def test_unavailable_first_page_stays_a_failure_when_the_setting_is_on(app_role_factory, registry, runs, fast_cfg):
    class Down:
        def fetch(self, url):
            raise PageLoadError("timeout")

        def wait_for_captcha_solved(self):
            return None

    src, _ = new_scripted(app_role_factory, registry, mk_pages(2, prefix="B0T"))
    claim, rep = go(runs, app_role_factory, Down(), src.source_id)
    assert (rep.outcome, rep.error_code, rep.repeat_check_pages) == ("FAILED", "PAGE_LOAD_FAILED", 0)


def test_finish_drop_cursor_is_only_for_truncated_and_closes_without_a_cursor(app_role_factory, registry, runs, fast_cfg):
    src, site = new_scripted(app_role_factory, registry, mk_pages(3, prefix="B0U"))
    c = runs.claim("US", src.source_id)
    with pytest.raises(ValueError):
        runs.finish(c, "SUCCESS_NONEMPTY", drop_cursor=True)
    runs.finish(c, "SUCCESS_TRUNCATED", truncation_reason="DEPTH_LIMIT", pages_fetched=0, drop_cursor=True)
    r = full_row(app_role_factory, c.run_id)
    assert (r.outcome, r.cycle_state, r.next_url, r.next_page_no, r.truncation_reason) == ("SUCCESS_TRUNCATED", "CLOSED_NO_CURSOR", None, None, "DEPTH_LIMIT")
