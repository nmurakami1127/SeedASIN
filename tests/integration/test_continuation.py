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
    assert runs.reap_expired("US") == [claim.run_id]
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
    runs.reap_expired("US")
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
