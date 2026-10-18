"""上限で打ち切った出典の続き取得（collector_run の cycle）の結合テスト。疑似ページ・使い捨てtest-db。外部接続なし。

確認すること: 続きの取得（取得済みページを繰り返さない）／ASIN保存と再開位置が同一トランザクション／中断（保存直前・直後）／
同時継続・旧トークン・期限切れ・除外・保留の拒否／重なり確認(overlap)・無効な次ページ・ループ／一巡完了から30日／
一時的な失敗は一巡を完了にしない／BROKENの取り直しの連続上限／旧方式の行と既存の振る舞いが変わらないこと／DBの制約。
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from acquisition.lease import LeaseLost, NotEligible, PageCursor
from acquisition.registry import SourceRegistry
from asin_collector.source_runner import CaptchaNotSolved, FetchedPage, RunLimits, load_source, run_claimed_source, source_url
from tests.integration.acq_helpers import expire_run, insert_old_success, make_runs, new_source

pytestmark = pytest.mark.integration


def card(asins):
    return "".join(f'<div data-component-type="s-search-result" data-asin="{a}"></div>' for a in asins)


def mk_pages(n_pages: int, per: int = 3, prefix: str = "B0C") -> list[list[str]]:
    """n_pages ページの出品者一覧（ページごとに別のASIN）。1始まりのページ番号 = 配列の添字+1。"""
    return [[f"{prefix}{i:03d}{j:04d}"[:10] for j in range(per)] for i in range(1, n_pages + 1)]


class Crash(BaseException):
    """プロセス死の模擬（Exceptionではないので、取得処理の失敗処理に捕捉されない）。"""


class Site:
    """出典の検索結果（ページ番号ごとのASIN）を返す疑似サイト。次ページは `<開始URL>&page=N+1`。取得したURLを記録する。"""

    def __init__(self, pages: list[list[str]], base: str) -> None:
        self.pages, self.base, self.urls = pages, base, []
        self.hook = None
        self.captcha_pages: set[int] = set()
        self.override: dict[int, str] = {}

    def u(self, n: int) -> str:
        return self.base if n == 1 else f"{self.base}&page={n}"

    def num(self, url: str) -> int:
        m = re.search(r"[?&]page=(\d+)", url)
        return int(m.group(1)) if m else 1

    def link(self, n: int) -> str:
        return f'<a class="s-pagination-next" href="{self.u(n + 1)}">Next</a>' if n < len(self.pages) else ""

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        n = self.num(url)
        if self.hook:
            self.hook(n)
        if n in self.captcha_pages:
            return FetchedPage("<html><body>Enter the characters you see below</body></html>", url, 200)
        if n in self.override:
            return FetchedPage(self.override[n], url, 200)
        return FetchedPage(f"<html><body>{card(self.pages[n - 1])}{self.link(n)}</body></html>", url, 200)

    def wait_for_captcha_solved(self) -> None:
        raise CaptchaNotSolved("解かれない")

    def html(self, asins: list[str], n: int) -> str:
        return f"<html><body>{card(asins)}{self.link(n)}</body></html>"


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


@pytest.fixture()
def fast_cfg(app_role_factory):
    """冷却を無くして連続実行を試せるようにし、後で元に戻す。"""
    saved = {}
    for m in ("US", "JP"):
        with app_role_factory.get_session(m) as conn, conn.begin():
            for r in conn.execute(text("SELECT * FROM acquisition_interval_config WHERE marketplace = :m"), {"m": m}).mappings():
                saved[(m, r["source_type"])] = dict(r)
            conn.execute(text("UPDATE acquisition_interval_config SET retry_cooldown_minutes = 0, max_consecutive_failures = 50 WHERE marketplace = :m"), {"m": m})
    yield
    for (m, t), row_ in saved.items():
        cols = {k: v for k, v in row_.items() if k not in ("marketplace", "source_type")}
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text(f"UPDATE acquisition_interval_config SET {sets} WHERE marketplace = :mp AND source_type = :t"), {**cols, "mp": m, "t": t})


def new_site(factory, registry, pages):
    src = new_source(registry, "US")
    site = Site(pages, source_url("US", load_source(factory, "US", src.source_id)))
    return src, site


def go(runs, factory, site, source_id, pages=2):
    claim = runs.claim("US", source_id)
    rep = run_claimed_source(runs, factory, site, claim, limits=RunLimits(max_pages=pages), sleep=lambda s: None)
    return claim, rep


def row(factory, run_id):
    with factory.get_session("US") as conn, conn.begin():
        return conn.execute(text(
            "SELECT id, status, outcome, cycle_id, cycle_seq, cycle_state, continues_run_id, supersedes_cycle_id, cycle_start_reason, restart_streak, "
            "start_url, next_url, next_page_no, last_page_url, overlap_pages, overlap_ratio, drift_detected, cursor_error, error_code, asin_count, pages_fetched "
            "FROM collector_run WHERE id = :i"), {"i": run_id}).one()


def asins_of(factory, run_id) -> list[str]:
    with factory.get_session("US") as conn, conn.begin():
        return [r[0] for r in conn.execute(text("SELECT asin FROM collector_asin WHERE collector_run_id = :i ORDER BY id"), {"i": run_id})]


def decision(factory, source_id):
    with factory.get_session("US") as conn, conn.begin():
        f = load_facts(conn, "US", [source_id])[0]
        return decide(f, load_configs(conn, "US")[f.source_type], db_now(conn))


# ---------------------------------------------------------------- 続きの取得

def test_two_pages_then_the_next_run_continues_without_refetching_and_the_interval_starts_at_the_last_page(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(5))
    c1, r1 = go(runs, app_role_factory, site, src.source_id, pages=2)
    assert (r1.outcome, r1.pages) == ("SUCCESS_TRUNCATED", 2) and site.urls == [site.u(1), site.u(2)]
    a = row(app_role_factory, c1.run_id)
    assert (a.cycle_id, a.cycle_seq, a.cycle_state, a.cycle_start_reason) == (c1.run_id, 1, "OPEN", "FRESH")
    assert (a.next_url, a.next_page_no, a.last_page_url) == (site.u(3), 3, site.u(2))  # 保存済みの最後のページの次
    d = decision(app_role_factory, src.source_id)
    assert d.state == "ELIGIBLE_CONTINUATION" and d.plan == "CONTINUE" and d.overlap_pages == 1  # 30日を待たずに続けられる（打切りは起点にならない）
    assert d.last_success_at is None
    # 2回目: 前回の最後のページ(2)を取り直して確認 → 3,4 を保存。ページ1は取得しない
    site.urls.clear()
    c2, r2 = go(runs, app_role_factory, site, src.source_id, pages=2)
    assert site.urls == [site.u(2), site.u(3), site.u(4)] and (r2.pages, r2.overlap_pages, r2.drift) == (2, 1, False)
    b = row(app_role_factory, c2.run_id)
    assert (b.cycle_id, b.cycle_seq, b.continues_run_id, b.cycle_start_reason) == (c1.run_id, 2, c1.run_id, "CONTINUE")
    assert (b.start_url, b.next_url, b.overlap_pages, float(b.overlap_ratio), b.drift_detected) == (site.u(3), site.u(5), 1, 1.0, False)
    # 3回目: 最後のページ(5)まで → 一巡完了
    site.urls.clear()
    c3, r3 = go(runs, app_role_factory, site, src.source_id, pages=5)
    assert site.urls == [site.u(4), site.u(5)] and r3.outcome == "SUCCESS_NONEMPTY"
    z = row(app_role_factory, c3.run_id)
    assert (z.cycle_state, z.next_url, z.next_page_no, z.cycle_seq) == ("COMPLETE", None, None, 3)
    total = asins_of(app_role_factory, c1.run_id) + asins_of(app_role_factory, c2.run_id) + asins_of(app_role_factory, c3.run_id)
    assert len(total) == len(set(total)) == 15  # 取りこぼしも重複も無い（この疑似サイトでは。実際の検索結果の変化までは保証しない）
    d = decision(app_role_factory, src.source_id)
    assert d.state == "TOO_RECENT" and d.gate_basis == "RUN" and d.plan == "FRESH"  # 最後のページへ到達した時点から30日
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)


def test_asins_already_saved_in_the_cycle_are_not_saved_again(app_role_factory, registry, runs, fast_cfg):
    pages = mk_pages(3)
    pages[2] = pages[2][:2] + [pages[0][0]]  # 3ページ目に、1ページ目のASINが混ざる（検索結果の並びが動いた）
    src, site = new_site(app_role_factory, registry, pages)
    go(runs, app_role_factory, site, src.source_id, pages=2)
    c2, r2 = go(runs, app_role_factory, site, src.source_id, pages=5)
    assert r2.outcome == "SUCCESS_NONEMPTY"
    assert pages[0][0] not in asins_of(app_role_factory, c2.run_id)  # 同じ一巡で保存済みのASINは入れない
    assert len(asins_of(app_role_factory, c2.run_id)) == 2


# ---------------------------------------------------------------- 中断・保存の原子性

def test_asin_save_and_resume_position_are_one_transaction(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    claim = runs.claim("US", src.source_id)
    bad = PageCursor("https://x/2", None, "https://x/1", "a" * 64, ("B0A0000001",))  # next_urlだけでnext_page_noが無い → UPDATEがCHECK違反で失敗
    with pytest.raises(DBAPIError):
        runs.append_asins(claim, ["B0A0000001", "B0A0000002"], cursor=bad)
    assert asins_of(app_role_factory, claim.run_id) == [] and row(app_role_factory, claim.run_id).next_url is None  # ASINも位置も書かれない
    ok = PageCursor("https://x/2", 2, "https://x/1", "a" * 64, ("B0A0000001",))
    runs.append_asins(claim, ["B0A0000001"], cursor=ok)
    r = row(app_role_factory, claim.run_id)
    assert asins_of(app_role_factory, claim.run_id) == ["B0A0000001"] and r.next_url == "https://x/2"  # 両方書かれる


@pytest.mark.parametrize("crash_at", ["before_save", "after_save"])
def test_process_death_around_the_save_resumes_without_gap_or_duplicate(app_role_factory, registry, runs, fast_cfg, crash_at):
    src, site = new_site(app_role_factory, registry, mk_pages(4))
    claim = runs.claim("US", src.source_id)
    orig = runs.append_asins
    if crash_at == "before_save":
        site.hook = lambda n: (_ for _ in ()).throw(Crash()) if n == 3 else None  # 3ページ目の取得の途中で死ぬ（保存前）
    else:
        def saved_then_die(c, asins, pages=1, *, cursor=None):
            n = orig(c, asins, pages, cursor=cursor)
            if cursor and cursor.last_page_url == site.u(3):
                raise Crash()  # 3ページ目を保存した直後に死ぬ
            return n

        runs.append_asins = saved_then_die
    with pytest.raises(Crash):
        run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
    runs.append_asins = orig
    site.hook = None
    expire_run(app_role_factory, "US", claim.run_id)
    assert runs.reap_expired("US", src.source_id) == [claim.run_id]  # この出典だけ（他のテストの実行中の行に依存しない）
    r = row(app_role_factory, claim.run_id)
    assert (r.status, r.cycle_state) == ("INTERRUPTED", "OPEN")
    stored = 2 if crash_at == "before_save" else 3
    assert len(asins_of(app_role_factory, claim.run_id)) == 3 * stored  # 保存済みのページ数と位置が食い違わない
    assert (r.next_page_no, r.last_page_url) == (stored + 1, site.u(stored))
    site.urls.clear()
    c2, rep = go(runs, app_role_factory, site, src.source_id, pages=9)
    assert rep.outcome == "SUCCESS_NONEMPTY"
    assert site.urls == [site.u(stored)] + [site.u(i) for i in range(stored + 1, 5)]  # 重なり確認の1ページを除き、取得済みは取り直さない
    total = asins_of(app_role_factory, claim.run_id) + asins_of(app_role_factory, c2.run_id)
    assert len(total) == len(set(total)) == 12  # 欠落も重複も無い


def test_ctrl_c_interrupts_and_the_cycle_stays_open(app_role_factory, registry, runs):
    src, site = new_site(app_role_factory, registry, mk_pages(3))
    site.hook = lambda n: (_ for _ in ()).throw(KeyboardInterrupt()) if n == 2 else None
    claim = runs.claim("US", src.source_id)
    with pytest.raises(KeyboardInterrupt):
        run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
    r = row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.cycle_state, r.next_page_no) == ("INTERRUPTED", "INTERRUPTED", "OPEN", 2)
    assert decision(app_role_factory, src.source_id).state == "ELIGIBLE_CONTINUATION"


# ---------------------------------------------------------------- 拒否: 同時・旧トークン・期限切れ・除外・保留・冷却

def test_concurrent_continuation_old_token_and_expired_lease_are_rejected(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(4))
    go(runs, app_role_factory, site, src.source_id, pages=1)
    c2 = runs.claim("US", src.source_id)  # 続きが実行中
    assert c2.plan == "CONTINUE"
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)  # 同時の継続・新しい一巡は始められない
    expire_run(app_role_factory, "US", c2.run_id)
    runs.reap_expired("US", src.source_id)
    with pytest.raises(LeaseLost):
        runs.append_asins(c2, ["B0ZZZZZZ01"], cursor=PageCursor("https://x/9", 9, "https://x/8", "b" * 64, ("B0ZZZZZZ01",)))
    with pytest.raises(LeaseLost):
        runs.finish(c2, "SUCCESS_TRUNCATED", truncation_reason="PAGE_LIMIT")
    c3 = runs.claim("US", src.source_id)  # 回収後は、保存済みの位置から続けられる
    assert c3.plan == "CONTINUE" and c3.cycle_id == c2.cycle_id


def test_exclusion_hold_and_cooldown_still_gate_an_open_cycle(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(4))
    go(runs, app_role_factory, site, src.source_id, pages=1)
    registry.set_status("US", src.source_id, "EXCLUDED", "テスト")
    assert decision(app_role_factory, src.source_id).state == "EXCLUDED"
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)
    registry.release("US", src.source_id, "解除")
    registry.set_status("US", src.source_id, "ON_HOLD", "テスト")
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)
    registry.release("US", src.source_id, "解除")
    assert decision(app_role_factory, src.source_id).plan == "CONTINUE"  # 解除されれば、状態から再評価されて続けられる
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("UPDATE acquisition_interval_config SET retry_cooldown_minutes = 60 WHERE marketplace = 'US'"))
    site.captcha_pages = {2}
    c, rep = go(runs, app_role_factory, site, src.source_id, pages=2)
    assert rep.outcome == "FAILED" and row(app_role_factory, c.run_id).cycle_state == "OPEN"  # CAPTCHAは一巡を完了にも正常な0件にもしない
    assert decision(app_role_factory, src.source_id).state == "COOLDOWN"  # 失敗の冷却は続きにも適用される
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)


def test_transient_failures_keep_the_position_and_never_complete_the_cycle(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(4))
    go(runs, app_role_factory, site, src.source_id, pages=1)
    site.captcha_pages = {3}
    c2, rep = go(runs, app_role_factory, site, src.source_id, pages=5)  # 2ページ目は保存、3ページ目でCAPTCHA未解決
    assert rep.outcome == "PARTIAL" and rep.error_code == "CAPTCHA_UNSOLVED"
    r = row(app_role_factory, c2.run_id)
    assert (r.cycle_state, r.next_page_no, r.cursor_error) == ("OPEN", 3, None)  # 位置は保存済みの最後のページの次のまま
    site.captcha_pages = set()
    c3, rep3 = go(runs, app_role_factory, site, src.source_id, pages=5)
    assert rep3.outcome == "SUCCESS_NONEMPTY" and row(app_role_factory, c3.run_id).cycle_state == "COMPLETE"


# ---------------------------------------------------------------- 重なり確認・無効な位置・ループ・取り直し

def test_overlap_detects_drift_but_continues_and_a_big_mismatch_breaks_the_cycle(app_role_factory, registry, runs, fast_cfg):
    pages = mk_pages(4)
    src, site = new_site(app_role_factory, registry, pages)
    go(runs, app_role_factory, site, src.source_id, pages=2)
    # 2ページ目の中身が一部入れ替わった（検索結果の変化）→ 重なりは足りる → driftとして記録して続ける
    site.override[2] = site.html([pages[1][0], pages[1][1], "B0NEW00001"], 2)
    c2, rep = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert rep.drift is True and rep.outcome == "SUCCESS_TRUNCATED"
    r = row(app_role_factory, c2.run_id)
    assert r.drift_detected is True and 0.2 <= float(r.overlap_ratio) < 1.0 and r.cycle_state == "OPEN"
    # 最後に保存したページ(3)の中身が全く別になった（重なりが小さい）→ ページ送りの位置を信頼できない → BROKEN（完了にしない）
    site.override[3] = site.html(["B0OTHER001", "B0OTHER002", "B0OTHER003"], 3)
    c3, rep3 = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert (rep3.outcome, rep3.error_code) == ("PARTIAL", "OVERLAP_MISMATCH")
    z = row(app_role_factory, c3.run_id)
    assert (z.cycle_state, z.cursor_error, z.status) == ("BROKEN", "OVERLAP_MISMATCH", "FAILED")
    d = decision(app_role_factory, src.source_id)
    assert d.plan == "RESTART_BROKEN" and d.state == "ELIGIBLE_RESTART"
    site.override.clear()
    site.urls.clear()
    c4, _ = go(runs, app_role_factory, site, src.source_id, pages=1)  # 新しい一巡（先頭から）。理由を記録して旧cycleを置き換える
    y = row(app_role_factory, c4.run_id)
    assert site.urls == [site.u(1)]
    assert (y.cycle_seq, y.cycle_start_reason, y.supersedes_cycle_id, y.restart_streak) == (1, "RESTART_BROKEN", z.cycle_id, 1)
    assert y.cycle_id == c4.run_id and y.cycle_state == "OPEN"


def test_invalid_next_page_loop_and_repeated_page_are_never_normal_zero_or_complete(app_role_factory, registry, runs, fast_cfg):
    # (1) 続きのURLを開いたら結果が0件 → CURSOR_INVALID（SUCCESS_EMPTYにしない）
    src, site = new_site(app_role_factory, registry, mk_pages(3))
    go(runs, app_role_factory, site, src.source_id, pages=1)
    site.override[2] = "<html><body>該当なし</body></html>"
    c, rep = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert (rep.outcome, rep.error_code) == ("PARTIAL", "CURSOR_INVALID") and row(app_role_factory, c.run_id).cycle_state == "BROKEN"
    # (2) 次ページが直前のページを指す（ループ）→ REVISITED_URL
    s2, loop = new_site(app_role_factory, registry, mk_pages(3, prefix="B0D"))
    loop.override[2] = f'<html><body>{card(loop.pages[1])}<a class="s-pagination-next" href="{loop.u(1)}">Next</a></body></html>'
    c2, rep2 = go(runs, app_role_factory, loop, s2.source_id, pages=9)
    assert rep2.error_code == "REVISITED_URL" and row(app_role_factory, c2.run_id).cycle_state == "BROKEN"
    # (3) 別URLなのに同じ内容が繰り返される → REPEATED_PAGE
    pages3 = mk_pages(3, prefix="B0E")
    pages3[1] = list(pages3[0])
    s3, rep_site = new_site(app_role_factory, registry, pages3)
    c3, rep3 = go(runs, app_role_factory, rep_site, s3.source_id, pages=9)
    assert rep3.error_code == "REPEATED_PAGE" and row(app_role_factory, c3.run_id).cycle_state == "BROKEN"


def test_restart_after_broken_is_limited_so_the_same_state_is_not_refetched_forever(app_role_factory, registry, runs, fast_cfg):
    src = new_source(registry, "US")
    base = source_url("US", load_source(app_role_factory, "US", src.source_id))
    for i in range(3):  # 取得→(続きで重なりが合わず)BROKEN→先頭から取り直し、を繰り返す
        site = Site(mk_pages(4, prefix=f"B0F{i}"), base)
        go(runs, app_role_factory, site, src.source_id, pages=1)
        site.override[1] = site.html(["B0GONE%04d" % j for j in range(3)], 1)  # 前回の最後のページが別の内容になっている
        _, rep = go(runs, app_role_factory, site, src.source_id, pages=1)
        assert rep.error_code == "OVERLAP_MISMATCH"
    d = decision(app_role_factory, src.source_id)
    codes = [b.code for b in d.blockers]
    assert d.state == "ON_HOLD" and "ON_HOLD" in codes and "CYCLE_RESTART_LIMIT" in codes  # 上限に達したら自動保留（同じ状態で何度も取り直さない）
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT hold_reason FROM acquisition_source WHERE source_id = :s"), {"s": src.source_id}).scalar_one() == "AUTO_CYCLE_RESTARTS"
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)
    registry.release("US", src.source_id, "原因を確認した")  # 人が明示的に解除すれば、取り直せる（既存の保留解除と同じ操作）
    assert decision(app_role_factory, src.source_id).state == "ELIGIBLE_RESTART"


def test_expired_cycle_restarts_with_its_own_reason_and_is_never_complete():
    """純粋関数 decide: 一巡を14日放置したら EXPIRED（COMPLETEにしない）→ RESTART_EXPIRED。"""
    from datetime import datetime, timedelta, timezone

    from acquisition.eligibility import Config, RunFact, SourceFacts, resolve_cycle

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def run(days):
        return RunFact(7, "COMPLETED", "SUCCESS_TRUNCATED", now - timedelta(days=days), None, cycle_id=7, cycle_seq=1, cycle_state="OPEN",
                       next_url="u2", next_page_no=2, start_url="u1", last_page_url="u1", last_page_signature="s", last_page_asins=("A",), restart_streak=0)

    facts = SourceFacts(1, "US", "SELLER", "X", "", "h", "X", "", "ACTIVE", None, None, None, None, runs=[run(15)])
    cfg = Config()
    v = resolve_cycle(facts.runs, now, cfg)
    assert v.state == "EXPIRED" and v.resume_url == "u2"
    d = decide(facts, cfg, now)
    assert d.plan == "RESTART_EXPIRED" and d.state == "ELIGIBLE_RESTART" and d.last_success_at is None
    facts.runs[:] = [run(13)]
    assert decide(facts, cfg, now).plan == "CONTINUE"


def test_resolve_cycle_is_deterministic_across_many_runs():
    from datetime import datetime, timedelta, timezone

    from acquisition.eligibility import Config, RunFact, resolve_cycle

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def mk(rid, cid, seq, state, nxt, ago):
        return RunFact(rid, "COMPLETED" if state != "BROKEN" else "FAILED", "SUCCESS_TRUNCATED" if state == "OPEN" else "PARTIAL",
                       now - timedelta(days=ago), None, cycle_id=cid, cycle_seq=seq, cycle_state=state, next_url=nxt, next_page_no=seq + 1 if nxt else None,
                       start_url="s", last_page_url=f"p{rid}", last_page_signature="x", last_page_asins=("A",), restart_streak=0)

    runs = [mk(1, 1, 1, "BROKEN", None, 9), mk(5, 5, 1, "OPEN", "n5", 5), mk(6, 5, 2, "OPEN", "n6", 4), mk(9, 5, 3, "OPEN", "n9", 1)]
    for perm in (runs, list(reversed(runs)), runs[2:] + runs[:2]):
        v = resolve_cycle(list(perm), now, Config())
        assert (v.cycle_id, v.last_run_id, v.last_seq, v.state, v.resume_url, v.last_page_url) == (5, 9, 3, "OPEN", "n9", "p9")  # 並び順に依存しない


# ---------------------------------------------------------------- DBの制約

def test_database_constraints_reject_inconsistent_cycles(app_role_factory, registry, runs, fast_cfg):
    a, sa = new_site(app_role_factory, registry, mk_pages(4))
    b, sb = new_site(app_role_factory, registry, mk_pages(4, prefix="B0K"))
    ca, _ = go(runs, app_role_factory, sa, a.source_id, pages=1)
    cb, _ = go(runs, app_role_factory, sb, b.source_id, pages=1)
    ins = ("INSERT INTO collector_run (marketplace, run_type, target, status, source_id, attempt_token, lease_expires_at, lease_seconds, "
           "cycle_seq, cycle_id, continues_run_id, supersedes_cycle_id, cycle_start_reason, restart_streak, cycle_state) "
           "VALUES ('US', 'SELLER_STOREFRONT', 't', 'RUNNING', :sid, gen_random_uuid(), now() + interval '5 minutes', 300, :seq, :cid, :cont, :sup, :reason, :rs, 'OPEN')")

    def attempt(sid=None, **kw):
        params = {"sid": sid or a.source_id, "seq": 1, "cid": None, "cont": None, "sup": None, "reason": "FRESH", "rs": 0, **kw}
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text(ins), params)

    with pytest.raises(DBAPIError):
        attempt()  # OPENの一巡が残っている出典に、先頭からの新しい一巡は始められない
    with pytest.raises(DBAPIError):
        attempt(seq=2, cid=ca.run_id, cont=cb.run_id, reason="CONTINUE", rs=None)  # 別の出典のrunは続けられない（同じ出典・市場・cycleの連鎖）
    with pytest.raises(DBAPIError):
        attempt(seq=1, reason="RESTART_BROKEN", sup=ca.run_id, rs=1)  # OPENの一巡をBROKENの理由で置き換えられない
    with pytest.raises(DBAPIError):
        attempt(seq=3, cid=ca.run_id, cont=ca.run_id, reason="CONTINUE", rs=None)  # seqが連続していない
    done, sd = new_site(app_role_factory, registry, mk_pages(1, prefix="B0H"))
    cd, _ = go(runs, app_role_factory, sd, done.source_id, pages=5)
    assert row(app_role_factory, cd.run_id).cycle_state == "COMPLETE"
    with pytest.raises(DBAPIError):
        attempt(sid=done.source_id, seq=1, reason="RESTART_EXPIRED", sup=cd.run_id, rs=1)  # COMPLETEの一巡は置き換えられない
    with pytest.raises(DBAPIError):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("UPDATE collector_run SET next_url = 'x' WHERE id = :i"), {"i": cd.run_id})  # 終了後の行は書き換えられない（既存の保護）
    c3 = runs.claim("US", b.source_id)
    with pytest.raises(DBAPIError):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("UPDATE collector_run SET cycle_state = 'COMPLETE', status = 'FAILED', outcome = 'FAILED' WHERE id = :i"), {"i": c3.run_id})
    with pytest.raises(DBAPIError):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("UPDATE collector_run SET cycle_id = 1 WHERE id = :i"), {"i": c3.run_id})  # 一巡の識別は不変


# ---------------------------------------------------------------- 旧方式・既存の振る舞い

def test_legacy_rows_and_truncated_runs_without_a_position_are_unchanged(app_role_factory, registry, runs, fast_cfg):
    src = new_source(registry, "US")
    insert_old_success(app_role_factory, "US", src.source_id, 1, "SUCCESS_TRUNCATED")  # 次ページ位置の無い旧run（cycle列はNULL）
    d = decision(app_role_factory, src.source_id)
    assert d.state == "TOO_RECENT" and d.plan == "FRESH" and d.cycle is None  # 旧runは再開できるものとして扱わず、従来の30日制限のまま
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)
    old = new_source(registry, "US")
    insert_old_success(app_role_factory, "US", old.source_id, 40, "SUCCESS_TRUNCATED")  # 30日を過ぎた旧run
    assert decision(app_role_factory, old.source_id).state == "ELIGIBLE_REFETCH"
    c = runs.claim("US", old.source_id)  # 新しい一巡は先頭から（旧runにcycleが無いのでOPENの一巡は無い）
    assert (c.plan, c.cycle_seq) == ("FRESH", 1)
    runs.finish(c, "SUCCESS_TRUNCATED", truncation_reason="PAGE_LIMIT")  # 位置を保存していない打切り → 続き不可（CLOSED_NO_CURSOR）
    assert row(app_role_factory, c.run_id).cycle_state == "CLOSED_NO_CURSOR"
    assert decision(app_role_factory, old.source_id).state == "TOO_RECENT"


def test_cycle_settings_are_configurable_and_default_to_the_approved_values(app_role_factory, fast_cfg):
    from acquisition.cli import _cmd_set_cycle_config
    from acquisition.cli import Context  # noqa: F401
    import types

    with app_role_factory.get_session("US") as conn, conn.begin():
        assert tuple(conn.execute(text("SELECT cycle_max_age_days, overlap_pages, max_cycle_restarts FROM acquisition_interval_config "
                                       "WHERE marketplace = 'US' AND source_type = 'SELLER'")).one()) == (14, 1, 2)
    ctx = types.SimpleNamespace(app_factory=app_role_factory)
    a = types.SimpleNamespace(market="US", type="SELLER", cycle_max_age_days=7, overlap_pages=0, max_cycle_restarts=None)
    assert _cmd_set_cycle_config(a, ctx) == 0
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert tuple(conn.execute(text("SELECT cycle_max_age_days, overlap_pages, max_cycle_restarts FROM acquisition_interval_config "
                                       "WHERE marketplace = 'US' AND source_type = 'SELLER'")).one()) == (7, 0, 2)
    assert _cmd_set_cycle_config(types.SimpleNamespace(market="US", type="SELLER", cycle_max_age_days=999, overlap_pages=None, max_cycle_restarts=None), ctx) == 2  # 範囲外は拒否


# ================================================================ レビュー指摘への対応（DEVIATIONS #64、マイグレーション0018）

from sqlalchemy import create_engine  # noqa: E402

from acquisition.eligibility import ConfigError  # noqa: E402
from asin_collector.check_recommendations import NO_RESULTS_MARKER  # noqa: E402
from tests.integration.acq_helpers import set_config  # noqa: E402


def backdate(superuser_url, run_ids, days):
    """終了済みのrunの時刻を過去へ動かす（使い捨てtest-db専用。終了後の行は保護トリガーで書き換えられないため、このセッションだけトリガーを止める）。"""
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE collector_run SET started_at = started_at - make_interval(days => :d), "
                          "completed_at = completed_at - make_interval(days => :d) WHERE id = ANY(:ids)"), {"d": days, "ids": list(run_ids)})
    eng.dispose()


def run_ids_of_source(factory, source_id):
    with factory.get_session("US") as conn, conn.begin():
        return [r[0] for r in conn.execute(text("SELECT id FROM collector_run WHERE source_id = :s ORDER BY id"), {"s": source_id})]


def end_row(factory, run_id):
    with factory.get_session("US") as conn, conn.begin():
        return conn.execute(text("SELECT status, outcome, interrupt_kind, cycle_state, end_reached, next_url, last_page_url, error_code "
                                 "FROM collector_run WHERE id = :i"), {"i": run_id}).one()


def plan_input(factory, run_id, tmp_path):
    from tool_submission.planner import build_items

    items, files = build_items(factory, "US", run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    return items, [a for f in files for a in f.decode().split()]


# ---------------------------------------------------------------- 1. 最終ページ保存後・finish前の停止

@pytest.mark.parametrize("how", ["process_death", "lease_expired", "finish_failed"])
def test_stop_after_saving_the_last_page_finalizes_without_refetching_or_going_back_to_the_start(
        app_role_factory, registry, runs, fast_cfg, tmp_path, how):
    pages = mk_pages(3)
    src, site = new_site(app_role_factory, registry, pages)
    c1, _ = go(runs, app_role_factory, site, src.source_id, pages=1)  # 1ページ目 → OPEN
    claim = runs.claim("US", src.source_id)  # 続き: 2,3ページ目（3ページ目が末尾）
    assert claim.plan == "CONTINUE"
    orig_append, orig_finish = runs.append_asins, runs.finish
    if how == "process_death":
        def append_then_die(c, asins, pages=1, *, cursor=None):
            n = orig_append(c, asins, pages, cursor=cursor)
            if cursor and cursor.end_reached:
                raise Crash()  # 末尾のページを保存した直後、finishの前にプロセスが死ぬ
            return n
        runs.append_asins = append_then_die
        with pytest.raises(Crash):
            run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
    elif how == "lease_expired":
        def append_then_expire(c, asins, pages=1, *, cursor=None):
            n = orig_append(c, asins, pages, cursor=cursor)
            if cursor and cursor.end_reached:
                expire_run(app_role_factory, "US", c.run_id)  # 保存後にleaseが切れた（finishは拒否される）
            return n
        runs.append_asins = append_then_expire
        rep = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
        assert rep.outcome == "LEASE_LOST"
    else:
        state = {"n": 0}

        def finish_fails_once(c, outcome, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("DB接続が切れた（疑似）")  # 終了確定そのものが失敗 → 既存の失敗処理がPARTIALで確定する
            return orig_finish(c, outcome, **kw)
        runs.finish = finish_fails_once
        rep = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9), sleep=lambda s: None)
        assert (rep.outcome, rep.error_code) == ("PARTIAL", "EXCEPTION")
    runs.append_asins, runs.finish = orig_append, orig_finish
    if how != "finish_failed":
        expire_run(app_role_factory, "US", claim.run_id)
        assert runs.reap_expired("US", src.source_id) == [claim.run_id]  # 回収は既存の経路（INTERRUPTED/LEASE_EXPIRED）
    r = end_row(app_role_factory, claim.run_id)
    assert r.cycle_state == "OPEN" and r.end_reached is True and r.next_url is None and r.last_page_url == site.u(3)
    assert r.status == ("FAILED" if how == "finish_failed" else "INTERRUPTED")
    d = decision(app_role_factory, src.source_id)
    assert (d.plan, d.state, d.overlap_pages) == ("FINALIZE", "ELIGIBLE_CONTINUATION", 0)
    site.urls.clear()
    c3, rep3 = go(runs, app_role_factory, site, src.source_id, pages=5)
    assert site.urls == []  # 先頭にも途中にも戻らず、ページを開かない
    assert (rep3.outcome, rep3.plan, rep3.pages, rep3.cycle_id) == ("SUCCESS_NONEMPTY", "FINALIZE", 0, c1.run_id)
    z = row(app_role_factory, c3.run_id)
    assert (z.cycle_state, z.cycle_seq, z.continues_run_id, z.cycle_start_reason) == ("COMPLETE", 3, claim.run_id, "CONTINUE")
    assert end_row(app_role_factory, claim.run_id) == r  # 前のrunの行は変わらない（終了後の行は不変。旧プロセスの書き込みを復活させない）
    with pytest.raises(LeaseLost):
        runs.append_asins(claim, ["B0LATE0001"], cursor=PageCursor(None, None, site.u(3), "c" * 64, ("B0LATE0001",), end_reached=True))
    with pytest.raises(LeaseLost):
        runs.finish(claim, "SUCCESS_NONEMPTY")
    d = decision(app_role_factory, src.source_id)
    assert d.state == "TOO_RECENT" and d.gate_basis == "RUN" and d.plan == "FRESH"  # 一巡完了: ここから30日
    # 中断したrunが保存した末尾のページのASINは、次の使える取得runの入力に引き継がれる（どこからも投入されない、を防ぐ）
    items, sent = plan_input(app_role_factory, c3.run_id, tmp_path)
    assert sorted(sent) == sorted(pages[1] + pages[2]) and items[0].carried_from_runs == [claim.run_id]
    _, sent1 = plan_input(app_role_factory, c1.run_id, tmp_path)
    assert sent1 == pages[0]  # 1回目のrunは自分のASINだけ（引き継ぎ無し）


def test_null_next_url_alone_is_not_treated_as_the_end(app_role_factory, registry, runs, fast_cfg):
    """next_urlがNULLでも、末尾到達の記録(end_reached)が無ければ完了扱いにしない（1ページ目の失敗・保存前の中断）。"""
    src, site = new_site(app_role_factory, registry, mk_pages(2))
    site.captcha_pages = {1}
    c1, rep = go(runs, app_role_factory, site, src.source_id)
    r = end_row(app_role_factory, c1.run_id)
    assert rep.outcome == "FAILED" and (r.next_url, r.end_reached, r.cycle_state) == (None, None, "OPEN")
    assert decision(app_role_factory, src.source_id).plan == "RETRY_FIRST"
    s2, site2 = new_site(app_role_factory, registry, mk_pages(3, prefix="B0Q"))
    go(runs, app_role_factory, site2, s2.source_id, pages=1)
    c = runs.claim("US", s2.source_id)  # 続きのrunが1ページも保存せずに中断 → next_urlはNULLだが末尾ではない
    expire_run(app_role_factory, "US", c.run_id)
    runs.reap_expired("US", s2.source_id)
    assert end_row(app_role_factory, c.run_id).end_reached is None
    assert decision(app_role_factory, s2.source_id).plan == "CONTINUE"


def test_end_reached_requires_a_saved_last_page_without_a_next_url(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    claim = runs.claim("US", src.source_id)
    with pytest.raises(ValueError):
        runs.append_asins(claim, ["B0A0000009"], cursor=PageCursor("https://x/2", 2, "https://x/1", "a" * 64, ("B0A0000009",), end_reached=True))
    with pytest.raises(DBAPIError):  # アプリを通さない書き込みもDBの制約で拒否する
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(text("UPDATE collector_run SET end_reached = true WHERE id = :i"), {"i": claim.run_id})  # 最後に保存したページが無い
    assert asins_of(app_role_factory, claim.run_id) == []


# ---------------------------------------------------------------- 2. 取り直しの上限・期限切れ・解除

def test_expired_cycles_restart_through_claim_and_the_trigger_without_counting_toward_the_limit(
        app_role_factory, registry, runs, fast_cfg, superuser_url):
    src, site = new_site(app_role_factory, registry, mk_pages(4))
    for i in range(4):  # 放置による期限切れを、上限(2)を超えて繰り返しても止めない（取得の失敗ではない）
        c, rep = go(runs, app_role_factory, site, src.source_id, pages=1)
        assert rep.outcome == "SUCCESS_TRUNCATED"
        y = row(app_role_factory, c.run_id)
        if i:
            assert (y.cycle_start_reason, y.restart_streak, y.cycle_seq) == ("RESTART_EXPIRED", 0, 1)  # 取り直しの連続回数は増やさない
        backdate(superuser_url, [c.run_id], 15)
        d = decision(app_role_factory, src.source_id)
        assert (d.plan, d.state) == ("RESTART_EXPIRED", "ELIGIBLE_RESTART") and not d.blockers
    with app_role_factory.get_session("US") as conn, conn.begin():
        chain = conn.execute(text("SELECT supersedes_cycle_id FROM collector_run WHERE source_id = :s AND cycle_seq = 1 ORDER BY id"),
                             {"s": src.source_id}).scalars().all()
    assert chain[0] is None and all(x is not None for x in chain[1:])  # 旧一巡は完了にせず、理由つきで置き換えた（DBのトリガーを通過）


def _broken_cycle(runs, factory, base, source_id, tag):
    """1ページ取得 → 続きで重なりが合わずBROKEN、を1回起こす。"""
    site = Site(mk_pages(4, prefix=f"B0{tag}"), base)
    go(runs, factory, site, source_id, pages=1)
    site.override[1] = site.html([f"B0X{tag}{j:05d}"[:10] for j in range(3)], 1)
    _, rep = go(runs, factory, site, source_id, pages=1)
    assert rep.error_code == "OVERLAP_MISMATCH"


def test_limit_is_reached_only_by_broken_restarts_then_held_and_release_does_not_stop_it_again_on_the_same_history(
        app_role_factory, registry, runs, fast_cfg, superuser_url):
    src = new_source(registry, "US")
    base = source_url("US", load_source(app_role_factory, "US", src.source_id))
    _broken_cycle(runs, app_role_factory, base, src.source_id, "R1")  # streak 0 → BROKEN
    _broken_cycle(runs, app_role_factory, base, src.source_id, "R2")  # streak 1 → BROKEN
    # 途中で放置による期限切れを挟んでも、連続回数は増えも減りもしない
    site = Site(mk_pages(4, prefix="B0R3"), base)
    c, _ = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert row(app_role_factory, c.run_id).restart_streak == 2
    backdate(superuser_url, [c.run_id], 15)
    c2, _ = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert (row(app_role_factory, c2.run_id).cycle_start_reason, row(app_role_factory, c2.run_id).restart_streak) == ("RESTART_EXPIRED", 2)
    site.override[1] = site.html(["B0XGONE001", "B0XGONE002", "B0XGONE003"], 1)
    _, rep = go(runs, app_role_factory, site, src.source_id, pages=1)  # 3回目のBROKEN → 上限
    assert rep.error_code == "OVERLAP_MISMATCH" and rep.auto_held
    with app_role_factory.get_session("US") as conn, conn.begin():
        st = conn.execute(text("SELECT status, hold_reason FROM acquisition_source WHERE source_id = :s"), {"s": src.source_id}).one()
    assert tuple(st) == ("ON_HOLD", "AUTO_CYCLE_RESTARTS")
    d = decision(app_role_factory, src.source_id)
    assert d.state == "ON_HOLD" and "CYCLE_RESTART_LIMIT" in [b.code for b in d.blockers]
    registry.release("US", src.source_id, "原因を確認した")
    d = decision(app_role_factory, src.source_id)
    assert (d.state, d.plan) == ("ELIGIBLE_RESTART", "RESTART_BROKEN") and not d.blockers
    _broken_cycle(runs, app_role_factory, base, src.source_id, "R4")  # 解除後の取り直しは0から: もう一度BROKENでも、同じ履歴で即座に止めない
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT status FROM acquisition_source WHERE source_id = :s"), {"s": src.source_id}).scalar_one() == "ACTIVE"
        streaks = conn.execute(text("SELECT restart_streak FROM collector_run WHERE source_id = :s AND cycle_seq = 1 ORDER BY id"),
                               {"s": src.source_id}).scalars().all()
    assert streaks[-1] == 0
    assert decision(app_role_factory, src.source_id).state == "ELIGIBLE_RESTART"


def test_a_limit_shown_on_an_active_source_can_be_released_with_the_usual_command(app_role_factory, registry, runs, fast_cfg):
    """自動保留を経ずに上限に達した出典（上限の設定を下げた等）も、案内どおり release で解除できる。保留でも上限でもない出典の release は従来どおり拒否。"""
    src = new_source(registry, "US")
    base = source_url("US", load_source(app_role_factory, "US", src.source_id))
    _broken_cycle(runs, app_role_factory, base, src.source_id, "S1")
    _broken_cycle(runs, app_role_factory, base, src.source_id, "S2")  # streak 1 → BROKEN（上限2なので保留しない）
    set_config(app_role_factory, "US", "SELLER", max_cycle_restarts=1)  # 上限を下げた
    d = decision(app_role_factory, src.source_id)
    assert d.state == "CYCLE_RESTART_LIMIT"
    with pytest.raises(NotEligible):
        runs.claim("US", src.source_id)
    registry.release("US", src.source_id, "上限を下げたので確認した")
    d = decision(app_role_factory, src.source_id)
    assert (d.state, d.plan) == ("ELIGIBLE_RESTART", "RESTART_BROKEN")
    c = runs.claim("US", src.source_id)
    assert row(app_role_factory, c.run_id).restart_streak == 0
    with app_role_factory.get_session("US") as conn, conn.begin():
        ev = conn.execute(text("SELECT event_type, detail FROM acquisition_source_event WHERE source_id = :s ORDER BY event_id DESC LIMIT 1"),
                          {"s": src.source_id}).one()
    assert ev.event_type == "RELEASE" and "CYCLE_RESTART_LIMIT" in ev.detail
    other = new_source(registry, "US")
    with pytest.raises(ValueError, match="保留・除外されていません"):
        registry.release("US", other.source_id, "対象外")


# ---------------------------------------------------------------- 3. 保存済みページが無い一巡の再試行

def test_a_cycle_without_saved_pages_retries_from_the_first_page_and_a_verified_zero_completes_it(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(2))
    site.captcha_pages = {1}
    c1, rep = go(runs, app_role_factory, site, src.source_id)  # 1ページ目でCAPTCHA未解決 → 保存なしでOPEN
    assert rep.outcome == "FAILED"
    d = decision(app_role_factory, src.source_id)
    assert (d.plan, d.state, d.overlap_pages) == ("RETRY_FIRST", "ELIGIBLE_NEVER", 0)
    site.captcha_pages = set()
    site.override[1] = f'<html><body><div {NO_RESULTS_MARKER}>no results</div></body></html>'  # 出品が0件になった（確認済みの0件）
    site.urls.clear()
    c2, rep2 = go(runs, app_role_factory, site, src.source_id)
    assert (rep2.outcome, rep2.error_code, rep2.plan) == ("SUCCESS_EMPTY", None, "RETRY_FIRST")  # CURSOR_INVALIDにしない
    assert site.urls == [site.u(1)]
    z = row(app_role_factory, c2.run_id)
    assert (z.cycle_state, z.cycle_seq, z.continues_run_id, z.cycle_id, z.start_url) == ("COMPLETE", 2, c1.run_id, c1.run_id, site.u(1))
    assert rep2.cycle_id == c1.run_id
    assert decision(app_role_factory, src.source_id).state == "TOO_RECENT"


def test_retry_first_with_results_saves_from_page_one_while_a_mid_cycle_continuation_still_rejects_an_empty_cursor(
        app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(3))
    site.captcha_pages = {1}
    go(runs, app_role_factory, site, src.source_id)
    site.captcha_pages = set()
    site.urls.clear()
    c, rep = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert (rep.plan, rep.outcome, rep.overlap_pages) == ("RETRY_FIRST", "SUCCESS_TRUNCATED", 0) and site.urls == [site.u(1)]
    site.override[2] = "<html><body>該当なし</body></html>"
    _, rep2 = go(runs, app_role_factory, site, src.source_id, pages=1)  # 保存済みページがある途中の続きは、0件を位置の無効として扱う（従来どおり）
    assert (rep2.plan, rep2.error_code) == ("CONTINUE", "CURSOR_INVALID")


# ---------------------------------------------------------------- overlap_pages 0/1 と 2以上の拒否・cycle_id

def test_overlap_zero_skips_the_recheck_and_values_above_one_are_rejected(app_role_factory, registry, runs, fast_cfg, superuser_url):
    import types

    from acquisition.cli import _cmd_set_cycle_config

    set_config(app_role_factory, "US", "SELLER", overlap_pages=0)
    src, site = new_site(app_role_factory, registry, mk_pages(6))
    go(runs, app_role_factory, site, src.source_id, pages=2)
    assert decision(app_role_factory, src.source_id).overlap_pages == 0
    site.urls.clear()
    c2, rep = go(runs, app_role_factory, site, src.source_id, pages=2)
    assert site.urls == [site.u(3), site.u(4)] and rep.overlap_pages == 0 and row(app_role_factory, c2.run_id).overlap_pages is None
    ctx = types.SimpleNamespace(app_factory=app_role_factory)
    for bad in (2, 3):
        a = types.SimpleNamespace(market="US", type="SELLER", cycle_max_age_days=None, overlap_pages=bad, max_cycle_restarts=None)
        assert _cmd_set_cycle_config(a, ctx) == 2  # 黙って1に丸めない
    with pytest.raises(DBAPIError):  # CLIを通さない書き込みもDBの制約で拒否する
        set_config(app_role_factory, "US", "SELLER", overlap_pages=2)
    # 制約より前から2以上が入っていた環境（0018はNOT VALIDで付け、値を書き換えない）: 読み込み時に設定エラーとして止める
    eng = create_engine(superuser_url)
    with eng.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("ALTER TABLE acquisition_interval_config DROP CONSTRAINT ck_aic_overlap_pages"))
        conn.execute(text("UPDATE acquisition_interval_config SET overlap_pages = 2 WHERE marketplace = 'US' AND source_type = 'SELLER'"))
        conn.execute(text("ALTER TABLE acquisition_interval_config ADD CONSTRAINT ck_aic_overlap_pages CHECK (overlap_pages BETWEEN 0 AND 1) NOT VALID"))
    eng.dispose()
    with pytest.raises(ConfigError, match="overlap_pages=2"):
        decision(app_role_factory, src.source_id)
    with pytest.raises(ConfigError):
        runs.claim("US", src.source_id)
    a = types.SimpleNamespace(market="US", type="SELLER", cycle_max_age_days=None, overlap_pages=1, max_cycle_restarts=None)
    assert _cmd_set_cycle_config(a, ctx) == 0  # 案内どおり0/1へ直せば使える
    assert decision(app_role_factory, src.source_id).plan == "CONTINUE"


def test_claim_returns_the_cycle_id_confirmed_by_the_database(app_role_factory, registry, runs, fast_cfg):
    src, site = new_site(app_role_factory, registry, mk_pages(3))
    c1 = runs.claim("US", src.source_id)
    assert c1.cycle_id == c1.run_id == row(app_role_factory, c1.run_id).cycle_id  # 新しい一巡: トリガーが確定したid
    rep = run_claimed_source(runs, app_role_factory, site, c1, limits=RunLimits(max_pages=1), sleep=lambda s: None)
    assert rep.cycle_id == c1.run_id
    c2, rep2 = go(runs, app_role_factory, site, src.source_id, pages=1)
    assert c2.cycle_id == rep2.cycle_id == c1.run_id
