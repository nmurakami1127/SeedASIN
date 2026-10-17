"""出典管理に接続した取得（asin_collector.source_runner）の結合テスト。

外部へは一切接続しない: ページは疑似（FakeFetcher）、DBは使い捨てtest-db。実ページで未検証の挙動
（KEYWORD/BRANDの0件ウィジェット、配送先表示、URL形式）は、ここでは「検証済み」とは扱わず、
未検証の種別は UNVERIFIED_EMPTY にする振る舞いだけを確認する。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from acquisition.lease import LeaseLost, NotEligible
from acquisition.plan import build_plan
from acquisition.registry import SourceRegistry
from asin_collector.source_runner import (
    FetchedPage, PageLoadError, RunLimits, load_source, run_batch, run_claimed_source, select_targets, source_url,
)
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import asin_count, expire_run, make_active_report, make_runs, new_source, run_row, unique_key

pytestmark = pytest.mark.integration

NO_RESULTS = '<html><body><div data-csa-c-content-id="messaging-messages-no-results">No results</div></body></html>'
CAPTCHA = "<html><body>Enter the characters you see below</body></html>"
ERROR_503 = '<html><body><div id="cs_503_logo"></div></body></html>'
UNKNOWN = "<html><body><p>想定外の構造</p></body></html>"


def results(asins, next_href=None) -> str:
    cards = "".join(f'<div data-component-type="s-search-result" data-asin="{a}"></div>' for a in asins)
    nxt = f'<a class="s-pagination-next" href="{next_href}">Next</a>' if next_href else ""
    return f"<html><body>{cards}{nxt}</body></html>"


class FakeFetcher:
    """台本どおりに応答する疑似ブラウザ。台本の要素は HTML文字列 / FetchedPage / 例外 / callable(url)->上記。"""

    def __init__(self, *script, on_captcha=None):
        self.script = list(script)
        self.urls: list[str] = []
        self.captcha_waits = 0
        self._on_captcha = on_captcha

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        if not self.script:
            raise AssertionError(f"台本にない取得です: {url}")
        item = self.script.pop(0)
        if callable(item):
            item = item(url)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, FetchedPage) else FetchedPage(item, url, 200)

    def wait_for_captcha_solved(self) -> None:
        self.captcha_waits += 1
        if self._on_captcha:
            self._on_captcha()


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


@pytest.fixture()
def restore_config(app_role_factory):
    """設定を変えるテストの後始末（他のテストへ影響させない）。"""
    saved = {}
    for m in ("US", "JP"):
        with app_role_factory.get_session(m) as conn, conn.begin():
            for r in conn.execute(text("SELECT * FROM acquisition_interval_config WHERE marketplace = :m"), {"m": m}).mappings():
                saved[(m, r["source_type"])] = dict(r)
    yield
    for (m, t), row in saved.items():
        cols = {k: v for k, v in row.items() if k not in ("marketplace", "source_type")}
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text(f"UPDATE acquisition_interval_config SET {sets} WHERE marketplace = :mp AND source_type = :t"),
                         {**cols, "mp": m, "t": t})


def cfg(app_role_factory, market, source_type, **cols):
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with app_role_factory.get_session(market) as conn, conn.begin():
        conn.execute(text(f"UPDATE acquisition_interval_config SET {sets} WHERE marketplace = :mp AND source_type = :t"),
                     {**cols, "mp": market, "t": source_type})


def go(runs, app_role_factory, fetcher, market, source_id, **kw):
    claim = runs.claim(market, source_id)
    kw.setdefault("sleep", lambda s: None)
    return claim, run_claimed_source(runs, app_role_factory, fetcher, claim, **kw)


def decision(app_role_factory, market, source_id):
    with app_role_factory.get_session(market) as conn, conn.begin():
        f = load_facts(conn, market, [source_id])[0]
        return decide(f, load_configs(conn, market)[f.source_type], db_now(conn))


def collected(app_role_factory, market, run_id) -> list[str]:
    with app_role_factory.get_session(market) as conn, conn.begin():
        return [r[0] for r in conn.execute(text("SELECT asin FROM collector_asin WHERE collector_run_id = :i ORDER BY id"), {"i": run_id})]


# ---------------------------------------------------------------- 正常取得・URL・市場

@pytest.mark.parametrize("market,host,suffix", [("US", "www.amazon.com", "&marketplaceID=ATVPDKIKX0DER"), ("JP", "www.amazon.co.jp", "")])
def test_multi_page_success_saves_per_page_dedups_and_starts_the_thirty_day_interval(app_role_factory, registry, runs, market, host, suffix):
    src = new_source(registry, market)
    next1 = f"https://{host}/s?me=X&page=2"
    f = FakeFetcher(results(["B000000001", "B000000002"], next1), results(["B000000002", "B000000003"]))
    claim, rep = go(runs, app_role_factory, f, market, src.source_id)
    assert (rep.outcome, rep.pages, rep.asins) == ("SUCCESS_NONEMPTY", 2, 3)
    assert f.urls[0].startswith(f"https://{host}/s?me=") and f.urls[0].endswith(suffix)
    assert collected(app_role_factory, market, claim.run_id) == ["B000000001", "B000000002", "B000000003"]  # ページ内外の重複は1回だけ
    row = run_row(app_role_factory, market, claim.run_id)
    assert (row.status, row.outcome, row.completeness, row.pages_fetched, row.asin_count) == ("COMPLETED", "SUCCESS_NONEMPTY", "COMPLETE", 2, 3)
    d = decision(app_role_factory, market, src.source_id)
    assert d.state == "TOO_RECENT" and d.gate_basis == "RUN"  # 新方式の取得成功から30日空ける
    with pytest.raises(NotEligible):
        runs.claim(market, src.source_id)


def test_us_and_jp_runs_are_independent(app_role_factory, registry, runs):
    key = unique_key("AUSJP")
    us, jp = registry.register_source("US", "SELLER", key), registry.register_source("JP", "SELLER", key)
    fu, fj = FakeFetcher(results(["B0US000001"])), FakeFetcher(results(["B0JP000001"]))
    cu, ru = go(runs, app_role_factory, fu, "US", us.source_id)
    assert decision(app_role_factory, "JP", jp.source_id).eligible  # USの取得はJPの出典に影響しない
    cj, rj = go(runs, app_role_factory, fj, "JP", jp.source_id)
    assert collected(app_role_factory, "US", cu.run_id) == ["B0US000001"] and collected(app_role_factory, "JP", cj.run_id) == ["B0JP000001"]
    assert fu.urls[0].startswith("https://www.amazon.com/") and fj.urls[0].startswith("https://www.amazon.co.jp/")


# ---------------------------------------------------------------- 0件の扱い

def test_confirmed_empty_for_seller_counts_as_success_per_config(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", empty_counts_as_success=True)
    src = new_source(registry, "US")
    claim, rep = go(runs, app_role_factory, FakeFetcher(NO_RESULTS), "US", src.source_id)
    assert rep.outcome == "SUCCESS_EMPTY" and run_row(app_role_factory, "US", claim.run_id).completeness == "COMPLETE"
    assert decision(app_role_factory, "US", src.source_id).state == "TOO_RECENT"


def test_confirmed_empty_does_not_count_when_config_says_so(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", empty_counts_as_success=False)
    src = new_source(registry, "US")
    go(runs, app_role_factory, FakeFetcher(NO_RESULTS), "US", src.source_id)
    assert decision(app_role_factory, "US", src.source_id).state == "ELIGIBLE_NEVER"


def test_unverified_empty_is_never_a_success(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "KEYWORD", retry_cooldown_minutes=0)
    for t in ("SELLER", "BRAND_NAME"):
        cfg(app_role_factory, "US", t, retry_cooldown_minutes=0)
    # (種別, ページ, 期待するerror_code): 0件ウィジェットでの判定が実ページで検証済みなのはSELLERだけ。
    cases = [("SELLER", UNKNOWN, "NO_CARDS_NO_WIDGET"), ("KEYWORD", UNKNOWN, "NO_CARDS_NO_WIDGET"),
             ("KEYWORD", NO_RESULTS, "EMPTY_NOT_VERIFIED_FOR_TYPE"), ("BRAND_NAME", NO_RESULTS, "EMPTY_NOT_VERIFIED_FOR_TYPE")]
    for typ, page, code in cases:
        src = new_source(registry, "US", typ)
        claim, rep = go(runs, app_role_factory, FakeFetcher(page), "US", src.source_id)
        assert (rep.outcome, rep.error_code) == ("UNVERIFIED_EMPTY", code), (typ, code)
        row = run_row(app_role_factory, "US", claim.run_id)
        assert (row.status, row.outcome) == ("FAILED", "UNVERIFIED_EMPTY")
        d = decision(app_role_factory, "US", src.source_id)
        assert d.last_success_at is None and d.state == "ELIGIBLE_NEVER" and d.consecutive_failures == 1


# ---------------------------------------------------------------- 上限・部分・失敗

@pytest.mark.parametrize("counts,expected", [(True, "TOO_RECENT"), (False, "ELIGIBLE_NEVER")])
def test_page_limit_truncates_and_follows_the_truncated_setting(app_role_factory, registry, runs, restore_config, counts, expected):
    cfg(app_role_factory, "US", "SELLER", truncated_counts_as_success=counts)
    src = new_source(registry, "US")
    f = FakeFetcher(results(["B000000001"], "https://www.amazon.com/s?p=2"), results(["B000000002"], "https://www.amazon.com/s?p=3"))
    claim, rep = go(runs, app_role_factory, f, "US", src.source_id, limits=RunLimits(max_pages=2))
    assert (rep.outcome, rep.pages, len(f.urls)) == ("SUCCESS_TRUNCATED", 2, 2)  # 3ページ目は取得しない
    row = run_row(app_role_factory, "US", claim.run_id)
    assert (row.status, row.outcome, row.completeness) == ("COMPLETED", "SUCCESS_TRUNCATED", "TRUNCATED")
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT truncation_reason FROM collector_run WHERE id = :i"), {"i": claim.run_id}).scalar_one() == "PAGE_LIMIT"
    assert decision(app_role_factory, "US", src.source_id).state == expected


def test_asin_limit_truncates(app_role_factory, registry, runs):
    src = new_source(registry, "JP")
    f = FakeFetcher(results(["B000000001", "B000000002"], "https://www.amazon.co.jp/s?p=2"), results(["B000000003"]))
    _, rep = go(runs, app_role_factory, f, "JP", src.source_id, limits=RunLimits(max_pages=9, max_asins=2))
    assert rep.outcome == "SUCCESS_TRUNCATED" and len(f.urls) == 1


def test_server_error_is_retried_then_partial_keeps_saved_pages_and_is_not_a_success(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0)
    src = new_source(registry, "US")
    waits = []
    f = FakeFetcher(results(["B000000001"], "https://www.amazon.com/s?p=2"), ERROR_503, FetchedPage("x", "u", 503), ERROR_503)
    claim, rep = go(runs, app_role_factory, f, "US", src.source_id, sleep=waits.append)
    assert (rep.outcome, rep.error_code, rep.pages, rep.asins) == ("PARTIAL", "SERVER_ERROR", 1, 1)
    assert len(waits) == 2 and len(f.urls) == 4  # 2回まで自動リトライ
    assert collected(app_role_factory, "US", claim.run_id) == ["B000000001"]  # 保存済みのページは残る
    row = run_row(app_role_factory, "US", claim.run_id)
    assert (row.status, row.outcome, row.completeness) == ("FAILED", "PARTIAL", "PARTIAL")
    d = decision(app_role_factory, "US", src.source_id)
    assert d.last_success_at is None and d.consecutive_failures == 1


def test_transient_server_error_recovers_within_retries(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    f = FakeFetcher(ERROR_503, results(["B000000001"]))
    _, rep = go(runs, app_role_factory, f, "US", src.source_id)
    assert rep.outcome == "SUCCESS_NONEMPTY"


def test_first_page_failure_is_failed_and_three_in_a_row_auto_hold(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0, max_consecutive_failures=3)
    src = new_source(registry, "US")
    reps = [go(runs, app_role_factory, FakeFetcher(PageLoadError("timeout")), "US", src.source_id)[1] for _ in range(3)]
    assert [r.outcome for r in reps] == ["FAILED"] * 3 and reps[0].error_code == "PAGE_LOAD_FAILED"
    assert [r.auto_held for r in reps] == [False, False, True]
    with pytest.raises(NotEligible) as e:
        runs.claim("US", src.source_id)
    assert e.value.decision.state == "ON_HOLD"


def test_empty_page_after_results_and_revisited_url_are_partial(app_role_factory, registry, runs):
    s1, s2 = new_source(registry, "US"), new_source(registry, "US")
    f1 = FakeFetcher(results(["B000000001"], "https://www.amazon.com/s?p=2"), UNKNOWN)
    assert go(runs, app_role_factory, f1, "US", s1.source_id)[1].error_code == "EMPTY_PAGE_AFTER_RESULTS"
    same = lambda url: results(["B000000001"], url)  # 「次へ」が今のURLを指す
    f2 = FakeFetcher(same, same)
    rep = go(runs, app_role_factory, f2, "US", s2.source_id)[1]
    assert (rep.outcome, rep.error_code) == ("PARTIAL", "REVISITED_URL")


def test_unexpected_exception_is_recorded_without_stopping_the_caller(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    claim, rep = go(runs, app_role_factory, FakeFetcher(RuntimeError("boom")), "US", src.source_id)
    assert (rep.outcome, rep.error_code) == ("FAILED", "EXCEPTION")
    assert run_row(app_role_factory, "US", claim.run_id).status == "FAILED"


# ---------------------------------------------------------------- CAPTCHA・中断・lease

def test_captcha_wait_is_visible_in_db_then_resumes_and_succeeds(app_role_factory, registry, runs):
    src = new_source(registry, "JP")
    seen = {}
    holder = {}

    def while_waiting():
        row = run_row(app_role_factory, "JP", holder["run"])
        seen["status"], seen["lease"] = row.status, row.lease_seconds
        seen["expires_in"] = _expires_in(app_role_factory, "JP", holder["run"])

    f = FakeFetcher(CAPTCHA, results(["B000000001"]), on_captcha=while_waiting)
    claim = runs.claim("JP", src.source_id)
    holder["run"] = claim.run_id
    rep = run_claimed_source(runs, app_role_factory, f, claim, sleep=lambda s: None)
    assert seen["status"] == "CAPTCHA_WAIT" and seen["expires_in"] > claim.lease_seconds  # 待機中はDB上でも見え、leaseは長め
    assert (rep.outcome, f.captcha_waits) == ("SUCCESS_NONEMPTY", 1)
    assert run_row(app_role_factory, "JP", claim.run_id).status == "COMPLETED"


def _expires_in(factory, market, run_id) -> float:
    with factory.get_session(market) as conn, conn.begin():
        return float(conn.execute(text("SELECT extract(epoch FROM lease_expires_at - clock_timestamp()) FROM collector_run WHERE id = :i"),
                                  {"i": run_id}).scalar_one())


def test_unsolved_captcha_fails_after_the_round_limit(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    f = FakeFetcher(CAPTCHA, CAPTCHA, CAPTCHA, CAPTCHA)
    _, rep = go(runs, app_role_factory, f, "US", src.source_id, limits=RunLimits(max_captcha_rounds=3))
    assert (rep.outcome, rep.error_code, f.captcha_waits) == ("FAILED", "CAPTCHA_UNSOLVED", 3)


def test_ctrl_c_is_an_interrupt_that_is_not_counted_as_a_failure(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0, max_consecutive_failures=1)
    src = new_source(registry, "US")
    f = FakeFetcher(results(["B000000001"], "https://www.amazon.com/s?p=2"), KeyboardInterrupt())
    claim = runs.claim("US", src.source_id)
    with pytest.raises(KeyboardInterrupt):
        run_claimed_source(runs, app_role_factory, f, claim)
    row = run_row(app_role_factory, "US", claim.run_id)
    assert (row.status, row.outcome, row.interrupt_kind) == ("INTERRUPTED", "INTERRUPTED", "USER")
    assert collected(app_role_factory, "US", claim.run_id) == ["B000000001"]
    d = decision(app_role_factory, "US", src.source_id)
    assert d.consecutive_failures == 0 and d.state == "ELIGIBLE_NEVER"  # 中断は失敗に数えず、自動保留もしない


def test_a_run_that_lost_its_lease_writes_nothing_more_and_cannot_finish(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    holder = {}

    def expire_then_page(url):
        expire_run(app_role_factory, "US", holder["run"])  # 取得中にleaseが失効（heartbeatなし）
        return results(["B000000009"], "https://www.amazon.com/s?p=3")

    f = FakeFetcher(results(["B000000001"], "https://www.amazon.com/s?p=2"), expire_then_page, results(["B000000010"]))
    claim = runs.claim("US", src.source_id)
    holder["run"] = claim.run_id
    rep = run_claimed_source(runs, app_role_factory, f, claim, heartbeat_interval=3600)
    assert rep.outcome == "LEASE_LOST"
    assert len(f.urls) == 2  # 失効後は次のページを取得しない
    assert collected(app_role_factory, "US", claim.run_id) == ["B000000001"]  # 失効後のページは保存されない
    assert run_row(app_role_factory, "US", claim.run_id).outcome is None  # 旧プロセスは終了確定もしていない
    # 回収後に別プロセスが取得でき、古いclaimは何も書けない
    assert runs.reap_expired("US", src.source_id) == [claim.run_id]
    new_claim = runs.claim("US", src.source_id)
    with pytest.raises(LeaseLost):
        runs.append_asins(claim, ["B000000011"])
    with pytest.raises(LeaseLost):
        runs.finish(claim, "SUCCESS_NONEMPTY")
    assert new_claim.run_id != claim.run_id


def test_heartbeat_keeps_a_slow_fetch_alive(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", lease_seconds=2)
    src = new_source(registry, "US")

    def slow(url):
        time.sleep(3.2)  # lease(2秒)より長い取得。別スレッドのheartbeatが延長し続ける
        return results(["B000000001"])

    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, FakeFetcher(slow), claim, heartbeat_interval=0.3)
    assert rep.outcome == "SUCCESS_NONEMPTY"


def test_double_start_only_one_runs_and_the_other_is_refused(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    first = runs.claim("US", src.source_id)
    with pytest.raises(NotEligible) as e:
        runs.claim("US", src.source_id)
    assert e.value.decision.state == "RUNNING"
    items = run_batch(runs, app_role_factory, FakeFetcher(), "US", [src.source_id])
    assert items[0].result == "SKIPPED" and "RUNNING" in items[0].reason  # 実行中の出典は取得せず理由を残す
    runs.interrupt(first)


def test_concurrent_runners_never_both_fetch(app_role_factory, registry, runs):
    src = new_source(registry, "US")
    out, fetchers = [], []

    def worker():
        f = FakeFetcher(lambda url: (time.sleep(0.3), results(["B000000001"]))[1])
        fetchers.append(f)
        out.append(run_batch(runs, app_role_factory, f, "US", [src.source_id], heartbeat_interval=0.1)[0].result)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == ["RAN", "SKIPPED", "SKIPPED", "SKIPPED"] and sum(len(f.urls) for f in fetchers) == 1


def test_batch_skips_ineligible_and_stops_after_consecutive_failures(app_role_factory, registry, runs, restore_config):
    cfg(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0, max_consecutive_failures=99)
    ok = new_source(registry, "US")
    recent = new_source(registry, "US")
    go(runs, app_role_factory, FakeFetcher(results(["B000000001"])), "US", recent.source_id)
    a, b, c = (new_source(registry, "US") for _ in range(3))
    f = FakeFetcher(results(["B000000002"]), PageLoadError("x"), PageLoadError("y"), PageLoadError("z"))
    items = run_batch(runs, app_role_factory, f, "US", [ok.source_id, recent.source_id, a.source_id, b.source_id, c.source_id],
                      stop_after_failures=2)
    assert [(i.source_id, i.result) for i in items] == [(ok.source_id, "RAN"), (recent.source_id, "SKIPPED"),
                                                          (a.source_id, "RAN"), (b.source_id, "RAN")]  # 2連続で失敗→cは実行しない
    assert "TOO_RECENT" in items[1].reason


# ---------------------------------------------------------------- 起点・順位・対象の絞り込み

def test_origin_paths_rank_by_units_and_zero_unit_or_no_origin_are_not_expanded_by_default(tmp_path, app_role_factory, registry, runs):
    rid = make_active_report(app_role_factory, tmp_path, "JP", [("B0ORIGHIGH", 10, 1.0, 9), ("B0ORIGLOW1", 10, 1.0, 2), ("B0ORIGZERO", 10, 1.0, 0)])
    hi, lo, zero, none = (unique_key(p) for p in ("AHI", "ALO", "AZERO", "ANONE"))
    for key, asin in ((hi, "B0ORIGHIGH"), (lo, "B0ORIGLOW1"), (zero, "B0ORIGZERO")):
        registry.register_path("JP", "SELLER", key, origin_import_id=rid, origin_asin=asin)
    registry.register_source("JP", "SELLER", none)  # 起点なし
    plan = build_plan(app_role_factory, ReportImportService(app_role_factory, None), "JP", [rid])
    keys = lambda es: [e.facts.key_primary for e in es]
    only_ours = lambda es: [k for k in keys(es) if k in (hi.upper(), lo.upper(), zero.upper(), none.upper())]
    default = select_targets(plan, "IMPORT", 20)
    assert only_ours(default) == [hi.upper(), lo.upper()]  # 注文点数の多い順。0点・起点なしは対象外
    assert only_ours(select_targets(plan, "IMPORT", 20, include_zero_units=True))[:3] == [hi.upper(), lo.upper(), zero.upper()]
    assert none.upper() in keys(select_targets(plan, "IMPORT", 20, include_no_origin=True))
    assert len(select_targets(plan, "IMPORT", 1)) == 1  # 件数の上限
    with pytest.raises(ValueError):
        select_targets(plan, "IMPORT", 21)  # 無制限に広げられない
    # 経路は残っている（注文0点の候補も記録されたまま）
    with app_role_factory.get_session("JP") as conn, conn.begin():
        n = conn.execute(text("SELECT count(*) FROM acquisition_path p JOIN acquisition_source s USING (source_id) "
                              "WHERE s.key_primary = :k AND p.origin_units = 0"), {"k": zero.upper()}).scalar_one()
    assert n == 1


def test_source_urls_are_market_specific_and_store_urls_must_match_the_market(app_role_factory, registry):
    from asin_collector.source_runner import SourceRow

    mk = lambda t, p: SourceRow(1, t, p.upper() if t == "SELLER" else p, p, "")
    assert source_url("JP", mk("KEYWORD", "木工 道具")).startswith("https://www.amazon.co.jp/s?k=")
    assert source_url("US", mk("BRAND_NAME", "Sony")) == "https://www.amazon.com/s?rh=p_89%3ASony"
    assert source_url("US", mk("BRAND_STORE", "https://www.amazon.com/stores/Sony/page/ABC")).endswith("/page/ABC")
    with pytest.raises(ValueError):
        source_url("JP", mk("BRAND_STORE", "https://www.amazon.com/stores/Sony/page/ABC"))  # 市場の取り違え


def test_legacy_runs_without_source_id_are_not_touched_by_the_new_path(app_role_factory, registry, runs):
    with app_role_factory.get_session("US") as conn, conn.begin():
        legacy_id = conn.execute(text("INSERT INTO collector_run (marketplace, run_type, target, status) "
                                      "VALUES ('US', 'BRAND_PAGE', 'legacy-compat', 'RUNNING') RETURNING id")).scalar_one()
    src = new_source(registry, "US")
    go(runs, app_role_factory, FakeFetcher(results(["B000000001"])), "US", src.source_id)
    assert run_row(app_role_factory, "US", legacy_id).status == "RUNNING"  # 旧経路の行は変更されない
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("UPDATE collector_run SET status = 'COMPLETED', completed_at = now() WHERE id = :i"), {"i": legacy_id})


def test_load_source_rejects_other_market_ids(app_role_factory, registry):
    src = new_source(registry, "US")
    with pytest.raises(ValueError):
        load_source(app_role_factory, "JP", src.source_id)


def test_captcha_not_solved_in_time_ends_the_run_as_failed_without_evading(app_role_factory, registry, runs):
    from asin_collector.source_runner import CaptchaNotSolved

    class Waiting(FakeFetcher):
        def wait_for_captcha_solved(self):
            self.captcha_waits += 1
            raise CaptchaNotSolved("待機時間切れ")

    src = new_source(registry, "JP")
    f = Waiting(CAPTCHA)
    claim, rep = go(runs, app_role_factory, f, "JP", src.source_id)
    assert (rep.outcome, rep.error_code, len(f.urls)) == ("FAILED", "CAPTCHA_UNSOLVED", 1)  # 同じ対象を再取得しない
    assert run_row(app_role_factory, "JP", claim.run_id).status == "FAILED"
