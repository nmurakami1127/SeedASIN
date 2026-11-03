"""価格帯分割を組み込んだ出典取得（source_runner._crawl_banded）の結合テスト。疑似ページ・使い捨てtest-db。外部接続なし。

一巡＝セラー1件の全帯の走査。初期帯から始め、DEPTH_LIMITを確認した帯だけをその場で二分割する（docs/DEVIATIONS.md #83）。
確認: 遅延二分割の一連／最小幅でTRUNCATED_MIN／空の帯／帯をまたぐ重複ASIN／帯切替直後の偽OVERLAP_MISMATCHなし／NO_NEW_ASINS_LOOPにならない／
どこで中断しても続けられる再開（日次枠・lease喪失・プロセス死・帯の遷移の前後）／冪等性／エラー分離／既定OFFで従来動作。
"""
from __future__ import annotations

import math
import re
from urllib.parse import unquote

import pytest
from sqlalchemy import text

from acquisition.lease import LeaseLost
from acquisition.partition import PartitionConfig, PriceBandPartition, check_cover
from acquisition.partition import Band as PBand
from asin_collector.check_recommendations import NO_RESULTS_MARKER
from asin_collector.source_runner import (
    AccessBudgetExhausted, CaptchaNotSolved, FetchedPage, PageLoadError, RunLimits, load_source, run_batch, run_claimed_source, source_url,
)
from tests.integration.acq_helpers import expire_run, make_runs, new_source
from tests.integration.test_continuation import Crash, card, fast_cfg, registry, runs  # noqa: F401

pytestmark = pytest.mark.integration

STRAT = PriceBandPartition()
EDGES3 = (0, 1000, 2000)   # [0,1000) [1000,2000) [2000,inf)（円）
Y = 100                    # 円 → 価格の単位


class BandSite:
    """JPのストアフロント検索の疑似サイト。商品は (asin, 価格(円×100)の列) で、URLの p_36 の帯に含まれる価格を持つ商品が、価格の昇順で返る。
    1ページ per 件。検索結果の深さの限界は depth_pages ページ:
    - 繰り返しモード（既定）: depth_pages を超えると depth_pages ページ目と同じ内容が返り続ける（次へはある）。
    - end_at_depth=True: depth_pages ページ目で「次へ」が消える（繰り返しではなく打切り）。
    """

    def __init__(self, items, *, per: int = 3, depth_pages: int = 2, end_at_depth: bool = False) -> None:
        self.items = sorted(((a, tuple(p if isinstance(p, tuple) else (p,))) for a, p in items), key=lambda t: (min(t[1]), t[0]))
        self.per, self.depth, self.end_at_depth = per, depth_pages, end_at_depth
        self.urls: list[str] = []
        self.hook = None          # fetchごとに url を受け取る（中断の注入）
        self.faults: dict = {}    # (帯のlo, ページ番号) -> 'captcha' / 'http500' / 'load'（外すまで続く）

    @staticmethod
    def parse(url: str):
        band = STRAT.band_of_url(url)
        m = re.search(r"[?&]page=(\d+)", url)
        return band, (int(m.group(1)) if m else 1)

    def select(self, band) -> list[str]:
        return [a for a, ps in self.items if band is None or any(band.contains(p) for p in ps)]

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        if self.hook:
            self.hook(url)
        band, page = self.parse(url)
        fault = self.faults.get((band.lo if band else None, page))
        if fault == "captcha":
            return FetchedPage("<html><body>Enter the characters you see below</body></html>", url, 200)
        if fault == "http500":
            return FetchedPage('<html><body><div id="cs_503_logo"></div></body></html>', url, 503)
        if fault == "load":
            raise PageLoadError("timeout")
        sel = self.select(band)
        if not sel:
            return FetchedPage(f"<html><body><div {NO_RESULTS_MARKER}></div></body></html>", url, 200)
        total = math.ceil(len(sel) / self.per)
        eff = min(page, self.depth)
        content = sel[(eff - 1) * self.per: eff * self.per]
        if self.end_at_depth:
            has_next = page < min(total, self.depth)
        else:
            has_next = page < total
        base = re.sub(r"&page=\d+", "", url)
        nxt = f'<a class="s-pagination-next" href="{base}&page={page + 1}">Next</a>' if has_next else ""
        return FetchedPage(f"<html><body>{card(content)}{nxt}</body></html>", url, 200)

    def wait_for_captcha_solved(self) -> None:
        raise CaptchaNotSolved("解かれない")


class Router:
    """複数のセラーを1つのfetcherで返す（run_batch用）。URLの me= で振り分ける。"""

    def __init__(self, sites: dict[str, BandSite]) -> None:
        self.sites = sites

    def fetch(self, url: str):
        key = re.search(r"me=([^&]+)", url).group(1)
        return self.sites[unquote(key)].fetch(url)

    def wait_for_captcha_solved(self):
        raise CaptchaNotSolved("解かれない")


class Budget:
    def __init__(self, base, n: int) -> None:
        self.base, self.left = base, n

    def fetch(self, url):
        if self.left <= 0:
            raise AccessBudgetExhausted("日次枠切れ")
        self.left -= 1
        return self.base.fetch(url)

    def wait_for_captcha_solved(self):
        return self.base.wait_for_captcha_solved()


class FlakyRuns:
    """advance_band の m 回目の前/後でプロセス死（Crash）を起こす。"""

    def __init__(self, real, m: int, when: str) -> None:
        self.real, self.m, self.when, self.n = real, m, when, 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def advance_band(self, *a, **kw):
        self.n += 1
        if self.n == self.m and self.when == "before":
            raise Crash()
        r = self.real.advance_band(*a, **kw)
        if self.n == self.m and self.when == "after":
            raise Crash()
        return r


def cfg3(**kw) -> PartitionConfig:
    return PartitionConfig(initial_edges_yen=kw.pop("edges", EDGES3), **kw)


def limits(cfg=None, pages: int = 200, **kw) -> RunLimits:
    return RunLimits(max_pages=pages, close_on_repeated_page=True, price_band=cfg or cfg3(), **kw)


def new_jp(registry, key=None):
    return new_source(registry, "JP", "SELLER", key)


def base_of(factory, source_id) -> str:
    return source_url("JP", load_source(factory, "JP", source_id))


def go(runs, factory, site, source_id, *, lim=None, svc=None, sleep=lambda s: None):
    claim = runs.claim("JP", source_id)
    rep = run_claimed_source(svc or runs, factory, site, claim, limits=lim or limits(), sleep=sleep)
    return claim, rep


def run_row(factory, run_id):
    with factory.get_session("JP") as conn, conn.begin():
        return conn.execute(text(
            "SELECT status, outcome, completeness, truncation_reason, error_code, pages_fetched, asin_count, cycle_id, cycle_state, "
            "next_url, next_page_no, last_page_url, end_reached, interrupt_kind, start_url FROM collector_run WHERE id = :i"), {"i": run_id}).one()


def bands_of(factory, cycle_id):
    with factory.get_session("JP") as conn, conn.begin():
        return conn.execute(text(
            "SELECT band_id, lo_units, hi_units, parent_band_id, depth, state, close_reason, pages_fetched, asins_new "
            "FROM acquisition_band WHERE cycle_id = :c ORDER BY lo_units, depth, band_id"), {"c": cycle_id}).all()


def leaves(factory, cycle_id):
    return [b for b in bands_of(factory, cycle_id) if b.state != "SPLIT"]


def cycle_asins(factory, cycle_id) -> list[str]:
    with factory.get_session("JP") as conn, conn.begin():
        return [r[0] for r in conn.execute(text(
            "SELECT ca.asin FROM collector_asin ca JOIN collector_run r ON r.id = ca.collector_run_id WHERE r.cycle_id = :c ORDER BY ca.id"), {"c": cycle_id})]


def leaf_spans(factory, cycle_id):
    return [(b.lo_units, b.hi_units, b.state) for b in leaves(factory, cycle_id)]


def asin(i: int) -> str:
    return f"B0P{i:07d}"


def split_items():
    """band0: 4件 / band1 [1000,2000): 10件（深さ2ページ×3件=6件を超える→二分割） / band2: 3件。"""
    items = [(asin(1 + i), (100 + i * 100) * Y) for i in range(4)]                         # 100,200,300,400円
    items += [(asin(20 + i), (1000 + i * 100) * Y) for i in range(10)]                     # 1000..1900円
    items += [(asin(40 + i), (2500 + i * 100) * Y) for i in range(3)]                      # 2500..2700円
    return items


# ---------------------------------------------------------------- ① 遅延二分割の一連

def test_lazy_split_walks_every_band_in_order_and_completes_with_leaves_covering_the_range(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code, rep.truncation_reason, rep.completeness) == ("SUCCESS_NONEMPTY", None, None, None)
    assert (rep.pages, rep.asins, rep.band_splits, rep.band_min_width, rep.band_empty, rep.repeat_check_pages) == (9, 17, 1, 0, 0, 1)
    r = run_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.completeness, r.cycle_state, r.next_url, r.end_reached) == ("COMPLETED", "SUCCESS_NONEMPTY", "COMPLETE", "COMPLETE", None, True)
    assert r.start_url.endswith("&p_36=0-99999")  # 開始URLは最初の帯の1ページ目
    # 帯: 初期3つ + 親(SPLIT)の子2つ。割ったのは深さの限界を確認した帯だけ
    bs = bands_of(app_role_factory, claim.cycle_id)
    assert [(b.lo_units, b.hi_units, b.depth, b.state) for b in bs] == [
        (0, 100000, 0, "DONE"), (100000, 200000, 0, "SPLIT"), (100000, 140000, 1, "DONE"), (140000, 200000, 1, "DONE"), (200000, None, 0, "DONE")]
    assert bs[1].close_reason == "DEPTH_LIMIT" and bs[0].close_reason == "END_OF_RESULTS"
    assert check_cover([PBand(b.lo_units, b.hi_units) for b in leaves(app_role_factory, claim.cycle_id)], lo=0, hi=None)
    assert sorted(cycle_asins(app_role_factory, claim.cycle_id)) == sorted(a for a, _ in site.items)  # 全件・重複なし
    # 処理順は lo 昇順（各帯の最初のページ）
    order = []
    for u in site.urls:
        lo = BandSite.parse(u)[0].lo
        if lo not in order:
            order.append(lo)
    assert order == [0, 100000, 140000, 200000]  # 帯が初めて取得される順: lo昇順（分割した親の左の子は親と同じlo）
    # アクセス数 = 保存ページ + 重なり確認 + 繰り返しの検知と取り直し（2/回） + 空の帯の1ページ目
    assert len(site.urls) == rep.pages + rep.overlap_pages + 2 * rep.repeat_check_pages + rep.band_empty == 11


def test_children_whose_first_pages_are_all_already_seen_do_not_trip_no_new_asins_loop(app_role_factory, registry, runs, fast_cfg):
    # 左の子の2ページ（4件）は親が保存済みの6件に全て含まれる: 新しいASINが2ページ続けて0件でも、帯ありではループとみなさない
    src = new_jp(registry)
    site = BandSite(split_items())
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    left = [b for b in bands_of(app_role_factory, claim.cycle_id) if (b.lo_units, b.depth) == (100000, 1)][0]
    assert (left.pages_fetched, left.asins_new) == (2, 0)
    assert rep.outcome == "SUCCESS_NONEMPTY" and rep.error_code != "NO_NEW_ASINS_LOOP"


def test_a_band_that_fits_within_the_depth_is_never_split(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite([(asin(i), (1000 + i * 100) * Y) for i in range(6)])  # [1000,2000)に6件=ちょうど2ページ: 深さの限界に達しない
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.band_splits, rep.repeat_check_pages) == ("SUCCESS_NONEMPTY", 0, 0)
    assert [(b.state) for b in bands_of(app_role_factory, claim.cycle_id)] == ["DONE", "DONE", "DONE"]


# ---------------------------------------------------------------- ② 最小幅

def test_min_width_band_is_truncated_min_and_the_cycle_is_complete_but_marked_truncated(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    items = [(asin(i), 1500 * Y) for i in range(8)] + [(asin(50), 500 * Y), (asin(51), 2500 * Y)]  # 同じ価格が8件: 割っても分けられない
    site = BandSite(items)
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code, rep.truncation_reason, rep.completeness) == ("SUCCESS_NONEMPTY", None, "BAND_MIN_WIDTH", "TRUNCATED")
    assert rep.band_min_width == 1 and rep.band_splits >= 3
    r = run_row(app_role_factory, claim.run_id)
    assert (r.status, r.outcome, r.completeness, r.truncation_reason, r.cycle_state, r.next_url, r.end_reached) == \
        ("COMPLETED", "SUCCESS_NONEMPTY", "TRUNCATED", "BAND_MIN_WIDTH", "COMPLETE", None, True)
    lv = leaves(app_role_factory, claim.cycle_id)
    tm = [b for b in lv if b.state == "TRUNCATED_MIN"]
    assert len(tm) == 1 and (tm[0].lo_units, tm[0].hi_units) == (150000, 160000) and tm[0].close_reason == "DEPTH_LIMIT_MIN_WIDTH"
    assert check_cover([PBand(b.lo_units, b.hi_units) for b in lv], lo=0, hi=None)
    got = set(cycle_asins(app_role_factory, claim.cycle_id))
    assert {asin(50), asin(51)} <= got and len(got) == 2 + 6  # 打ち切った帯からは深さの範囲（2ページ=6件）まで
    assert len(cycle_asins(app_role_factory, claim.cycle_id)) == len(got)  # 親から子へ同じ商品を取り直しても二重に保存しない


def test_max_depth_stops_splitting_even_when_the_width_is_large(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite([(asin(i), (1000 + i * 10) * Y) for i in range(30)])
    claim, rep = go(runs, app_role_factory, site, src.source_id, lim=limits(cfg3(max_depth=1)))
    assert (rep.outcome, rep.completeness, rep.truncation_reason) == ("SUCCESS_NONEMPTY", "TRUNCATED", "BAND_MIN_WIDTH")
    assert max(b.depth for b in bands_of(app_role_factory, claim.cycle_id)) == 1


# ---------------------------------------------------------------- ③ 空の帯

def test_empty_bands_are_closed_as_empty_and_the_walk_continues(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite([(asin(i), (1000 + i * 100) * Y) for i in range(3)])  # band0 と band2 は空
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.band_empty, rep.pages, rep.asins) == ("SUCCESS_NONEMPTY", 2, 1, 3)
    bs = bands_of(app_role_factory, claim.cycle_id)
    assert [(b.state, b.close_reason, b.pages_fetched) for b in bs] == [("DONE", "EMPTY", 0), ("DONE", "END_OF_RESULTS", 1), ("DONE", "EMPTY", 0)]
    r = run_row(app_role_factory, claim.run_id)
    assert (r.cycle_state, r.end_reached) == ("COMPLETE", False)  # 最後の帯が空: 末尾のページが無いので end_reached は立たない（全帯が終端で完了を確定）
    assert len(site.urls) == rep.pages + rep.band_empty == 3  # 保存した1ページ + 空の帯の1ページ目 2回


def test_all_bands_empty_is_a_verified_empty_cycle(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    claim, rep = go(runs, app_role_factory, BandSite([]), src.source_id)
    assert (rep.outcome, rep.band_empty, rep.pages) == ("SUCCESS_EMPTY", 3, 0)
    assert run_row(app_role_factory, claim.run_id).cycle_state == "COMPLETE"


def test_first_page_without_cards_and_without_the_widget_is_not_treated_as_empty(app_role_factory, registry, runs, fast_cfg):
    class Blank(BandSite):
        def fetch(self, url):
            b, pg = self.parse(url)
            if b is not None and b.lo == 100000:
                self.urls.append(url)
                return FetchedPage("<html><body>想定外</body></html>", url, 200)
            return super().fetch(url)

    src = new_jp(registry)
    site = Blank([(asin(i), (500 + i * 100) * Y) for i in range(3)])  # band0 に3件、band1 は想定外のページ
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code) == ("PARTIAL", "NO_CARDS_NO_WIDGET")  # 保存済みがあるのでPARTIAL
    assert run_row(app_role_factory, claim.run_id).cycle_state == "OPEN"
    src2 = new_jp(registry)
    claim2, rep2 = go(runs, app_role_factory, Blank([]), src2.source_id, lim=limits(cfg3(edges=(100, 1000, 2000))))  # 保存したASINが無い一巡
    assert (rep2.outcome, rep2.error_code) == ("UNVERIFIED_EMPTY", "NO_CARDS_NO_WIDGET")  # 成功にも正常な0件にもしない


# ---------------------------------------------------------------- ④ 帯をまたぐ重複・帯切替

def test_an_asin_listed_in_two_bands_is_saved_once(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    items = [(asin(1), (500 * Y, 1500 * Y)), (asin(2), 600 * Y), (asin(3), 1600 * Y), (asin(4), 2500 * Y)]  # asin1 は価格が変わり2つの帯に出る
    claim, rep = go(runs, app_role_factory, BandSite(items), src.source_id)
    assert rep.outcome == "SUCCESS_NONEMPTY"
    got = cycle_asins(app_role_factory, claim.cycle_id)
    assert sorted(got) == [asin(1), asin(2), asin(3), asin(4)] and len(got) == 4


def test_a_band_whose_first_page_equals_the_previous_bands_last_page_is_not_a_repeat(app_role_factory, registry, runs, fast_cfg):
    # 隣り合う帯に同じ3商品が出る（価格が帯の境をまたいで変わった）: 帯の切替でprev_sigをリセットしないと「繰り返し」と誤検知して分割してしまう
    src = new_jp(registry)
    items = [(asin(i), (500 * Y, 1500 * Y)) for i in range(3)]
    claim, rep = go(runs, app_role_factory, BandSite(items), src.source_id)
    assert (rep.outcome, rep.band_splits, rep.repeat_check_pages, rep.asins) == ("SUCCESS_NONEMPTY", 0, 0, 3)
    assert len(cycle_asins(app_role_factory, claim.cycle_id)) == 3


def test_no_false_overlap_mismatch_right_after_a_band_switch(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=2))  # band0 の2ページ（末尾）で PAGE_LIMIT
    assert (r1.outcome, r1.truncation_reason, r1.pages) == ("SUCCESS_TRUNCATED", "PAGE_LIMIT", 2)
    a = run_row(app_role_factory, c1.run_id)
    assert (a.cycle_state, a.end_reached) == ("OPEN", False)  # 最後の帯の末尾ではない
    assert a.next_url.endswith("&p_36=100000-199999") and a.next_page_no == 1  # カーソルは次の帯の1ページ目
    site.urls.clear()
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert (r2.plan, r2.overlap_pages, r2.outcome, r2.error_code) == ("CONTINUE", 0, "SUCCESS_NONEMPTY", None)  # 帯が違うので重なり確認はしない
    assert BandSite.parse(site.urls[0])[0].lo == 100000 and BandSite.parse(site.urls[0])[1] == 1
    assert run_row(app_role_factory, c2.run_id).end_reached is True
    assert sorted(cycle_asins(app_role_factory, c1.cycle_id)) == sorted(a_ for a_, _ in site.items)


def test_overlap_check_still_runs_when_the_continuation_stays_in_the_same_band(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=3))  # band0(2ページ) + band1 の1ページ目
    assert r1.pages == 3
    site.urls.clear()
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert (r2.plan, r2.overlap_pages, r2.outcome) == ("CONTINUE", 1, "SUCCESS_NONEMPTY")
    assert site.urls[0].endswith("&p_36=100000-199999") and "page=2" not in site.urls[0]  # 重なり確認は最後に保存した1ページ目
    assert sorted(cycle_asins(app_role_factory, c1.cycle_id)) == sorted(a for a, _ in site.items)


def test_a_real_overlap_mismatch_within_a_band_still_breaks_the_cycle(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, _ = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=3))
    orig = site.fetch

    def swapped(url):
        b, pg = BandSite.parse(url)
        if b is not None and b.lo == 100000 and pg == 1:
            site.urls.append(url)
            return FetchedPage(f"<html><body>{card(['B0ZZ000001', 'B0ZZ000002', 'B0ZZ000003'])}</body></html>", url, 200)
        return orig(url)

    site.fetch = swapped
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert (r2.outcome, r2.error_code) == ("PARTIAL", "OVERLAP_MISMATCH")
    assert run_row(app_role_factory, c2.run_id).cycle_state == "BROKEN"


# ---------------------------------------------------------------- ⑤ 上限（ページ数/ASIN数）

def test_page_limit_keeps_the_cycle_open_and_continues_from_the_middle_of_a_band(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    total_runs = 0
    cycle = None
    while True:
        claim, rep = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=1))
        total_runs += 1
        cycle = claim.cycle_id
        assert total_runs < 40
        if rep.outcome != "SUCCESS_TRUNCATED":
            break
        r = run_row(app_role_factory, claim.run_id)
        assert (r.cycle_state, r.truncation_reason, r.end_reached) == ("OPEN", "PAGE_LIMIT", False)
    assert rep.outcome == "SUCCESS_NONEMPTY"
    assert sorted(cycle_asins(app_role_factory, cycle)) == sorted(a for a, _ in site.items)
    assert run_row(app_role_factory, claim.run_id).end_reached is True
    assert len(leaves(app_role_factory, cycle)) == 4


def test_asin_limit_closes_as_truncated_and_continues(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, site, src.source_id, lim=limits(max_asins=5))
    assert (r1.outcome, r1.truncation_reason) == ("SUCCESS_TRUNCATED", "ASIN_LIMIT")
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert r2.outcome == "SUCCESS_NONEMPTY"
    assert len(cycle_asins(app_role_factory, c1.cycle_id)) == 17


# ---------------------------------------------------------------- ⑥ 再開（帯の状態はDBが真実）

def _baseline(app_role_factory, registry, runs, items, cfg=None, **site_kw):
    src = new_jp(registry)
    site = BandSite(items, **site_kw)
    claim, rep = go(runs, app_role_factory, site, src.source_id, lim=limits(cfg))
    assert rep.outcome == "SUCCESS_NONEMPTY"
    return sorted(cycle_asins(app_role_factory, claim.cycle_id)), leaf_spans(app_role_factory, claim.cycle_id), len(site.urls), rep


def _finish_cycle(runs, app_role_factory, site, source_id, cfg=None, max_runs=12):
    last = None
    for _ in range(max_runs):
        claim, last = go(runs, app_role_factory, site, source_id, lim=limits(cfg))
        if last.outcome in ("SUCCESS_NONEMPTY", "SUCCESS_EMPTY"):
            return claim, last
        assert last.outcome != "PARTIAL", last
    raise AssertionError(f"完了しませんでした: {last}")


def _assert_same_as_baseline(app_role_factory, claim, base):
    asins, spans, _, _ = base
    got = cycle_asins(app_role_factory, claim.cycle_id)
    assert sorted(got) == asins and len(got) == len(set(got))  # 全件・二重保存なし
    assert leaf_spans(app_role_factory, claim.cycle_id) == spans
    assert check_cover([PBand(lo, hi) for lo, hi, _ in spans], lo=0, hi=None)
    assert run_row(app_role_factory, claim.run_id).cycle_state == "COMPLETE"
    bs = bands_of(app_role_factory, claim.cycle_id)
    assert not [b for b in bs if b.state in ("PENDING", "ACTIVE")]
    assert sum(b.pages_fetched for b in bs) == base[3].pages  # 保存済みの帯のページを取り直して二重に数えていない


@pytest.mark.parametrize("k", range(1, 12))
def test_daily_limit_at_every_fetch_then_continue(app_role_factory, registry, runs, fast_cfg, k):
    base = _baseline(app_role_factory, registry, runs, split_items())
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, Budget(site, k), src.source_id)
    if k < base[2]:
        assert r1.outcome == "DAILY_LIMIT"  # 失敗ではなく中断。位置は保存済み
        assert run_row(app_role_factory, c1.run_id).interrupt_kind == "DAILY_LIMIT"
    claim, last = _finish_cycle(runs, app_role_factory, site, src.source_id) if r1.outcome == "DAILY_LIMIT" else (c1, r1)
    _assert_same_as_baseline(app_role_factory, claim, base)


@pytest.mark.parametrize("k", range(1, 12))
def test_lease_lost_at_every_fetch_then_continue(app_role_factory, registry, runs, fast_cfg, k):
    base = _baseline(app_role_factory, registry, runs, split_items())
    src = new_jp(registry)
    site = BandSite(split_items())
    state = {"n": 0, "run": None}

    def hook(url):
        state["n"] += 1
        if state["n"] == k:
            expire_run(app_role_factory, "JP", state["run"])

    site.hook = hook
    claim = runs.claim("JP", src.source_id)
    state["run"] = claim.run_id
    rep = run_claimed_source(runs, app_role_factory, site, claim, limits=limits(), sleep=lambda s: None)
    site.hook = None
    assert rep.outcome == "LEASE_LOST"  # 取得の直後のDB操作で失効を検知し、以後は何も書かない
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    _assert_same_as_baseline(app_role_factory, c, base)


@pytest.mark.parametrize("k", range(1, 12))
def test_process_death_at_every_fetch_then_continue(app_role_factory, registry, runs, fast_cfg, k):
    base = _baseline(app_role_factory, registry, runs, split_items())
    src = new_jp(registry)
    site = BandSite(split_items())
    n = {"i": 0}

    def hook(url):
        n["i"] += 1
        if n["i"] == k:
            raise Crash()

    site.hook = hook
    claim = runs.claim("JP", src.source_id)
    crashed = False
    try:
        rep = run_claimed_source(runs, app_role_factory, site, claim, limits=limits(), sleep=lambda s: None)
    except Crash:
        crashed = True
    site.hook = None
    if crashed:
        expire_run(app_role_factory, "JP", claim.run_id)
        c, _ = _finish_cycle(runs, app_role_factory, site, src.source_id)
    else:
        c = claim
    _assert_same_as_baseline(app_role_factory, c, base)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("m", range(1, 10))
def test_process_death_around_every_band_transition_then_continue(app_role_factory, registry, runs, fast_cfg, m, when):
    base = _baseline(app_role_factory, registry, runs, split_items())
    src = new_jp(registry)
    site = BandSite(split_items())
    claim = runs.claim("JP", src.source_id)
    flaky = FlakyRuns(runs, m, when)
    with pytest.raises(Crash):
        run_claimed_source(flaky, app_role_factory, site, claim, limits=limits(), sleep=lambda s: None)
    expire_run(app_role_factory, "JP", claim.run_id)
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    _assert_same_as_baseline(app_role_factory, c, base)
    # 末尾到達(end_reached)を保存済みで止まった場合も、取得せずに完了として確定できる（FINALIZE）。最後の帯の末尾以外では立たない
    rows = run_rows_of_cycle(app_role_factory, c.cycle_id)
    assert [r.end_reached for r in rows if r.end_reached] == [True]


def run_rows_of_cycle(factory, cycle_id):
    with factory.get_session("JP") as conn, conn.begin():
        return conn.execute(text("SELECT id, end_reached, status, outcome FROM collector_run WHERE cycle_id = :c ORDER BY cycle_seq"), {"c": cycle_id}).all()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("m", range(1, 8))
def test_death_around_band_transitions_when_the_end_of_a_band_is_treated_as_the_depth_limit(app_role_factory, registry, runs, fast_cfg, m, when):
    cfg = cfg3(treat_end_as_depth_limit_pages=2)
    items = [(asin(i), (500 + i * 100) * Y) for i in range(2)] + [(asin(20 + i), (1000 + i * 50) * Y) for i in range(14)] + [(asin(60), 2500 * Y)]
    base = _baseline(app_role_factory, registry, runs, items, cfg, end_at_depth=True)
    assert base[3].band_splits >= 1
    src = new_jp(registry)
    site = BandSite(items, end_at_depth=True)
    claim = runs.claim("JP", src.source_id)
    flaky = FlakyRuns(runs, m, when)
    try:
        rep = run_claimed_source(flaky, app_role_factory, site, claim, limits=limits(cfg), sleep=lambda s: None)
        crashed = False
    except Crash:
        crashed = True
    if crashed:
        expire_run(app_role_factory, "JP", claim.run_id)
        c, _ = _finish_cycle(runs, app_role_factory, site, src.source_id, cfg)
    else:
        c = claim
    _assert_same_as_baseline(app_role_factory, c, base)


# ---------------------------------------------------------------- ⑦ treat_end_as_depth_limit_pages

def test_default_does_not_treat_a_vanished_next_link_as_the_depth_limit(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items(), end_at_depth=True)  # 限界で「次へ」が消える形でも、null（既定）なら帯の末尾として完了にする
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.band_splits, rep.completeness) == ("SUCCESS_NONEMPTY", 0, None)


def test_a_vanished_next_link_at_or_over_the_configured_pages_is_split_when_configured(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items(), end_at_depth=True)
    claim, rep = go(runs, app_role_factory, site, src.source_id, lim=limits(cfg3(treat_end_as_depth_limit_pages=2)))
    assert rep.outcome == "SUCCESS_NONEMPTY" and rep.band_splits >= 1
    assert check_cover([PBand(b.lo_units, b.hi_units) for b in leaves(app_role_factory, claim.cycle_id)], lo=0, hi=None)
    assert {a for a, _ in site.items} <= set(cycle_asins(app_role_factory, claim.cycle_id))


# ---------------------------------------------------------------- ⑧ 冪等性

def test_process_death_before_any_page_does_not_duplicate_bands_or_asins_on_retry(app_role_factory, registry, runs, fast_cfg):
    base = _baseline(app_role_factory, registry, runs, split_items())
    src = new_jp(registry)
    site = BandSite(split_items())
    site.hook = lambda url: (_ for _ in ()).throw(Crash())
    claim = runs.claim("JP", src.source_id)
    with pytest.raises(Crash):
        run_claimed_source(runs, app_role_factory, site, claim, limits=limits(), sleep=lambda s: None)
    site.hook = None
    assert len(bands_of(app_role_factory, claim.cycle_id)) == 3  # 初期の帯だけ。再実行しても増えない
    expire_run(app_role_factory, "JP", claim.run_id)
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    assert last.plan == "RETRY_FIRST" and c.cycle_id == claim.cycle_id
    _assert_same_as_baseline(app_role_factory, c, base)
    assert len(bands_of(app_role_factory, c.cycle_id)) == 5


def test_init_bands_is_idempotent_across_runs_of_the_same_cycle(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=4))
    n1 = len(bands_of(app_role_factory, c1.cycle_id))
    c2, r2 = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=1))
    assert c2.cycle_id == c1.cycle_id
    assert len(bands_of(app_role_factory, c2.cycle_id)) >= n1
    ids = [b.band_id for b in bands_of(app_role_factory, c2.cycle_id)]
    assert len(ids) == len(set(ids))
    spans = [(b.lo_units, b.hi_units) for b in bands_of(app_role_factory, c2.cycle_id)]
    assert len(spans) == len(set(spans))


# ---------------------------------------------------------------- ⑨ エラー分離

@pytest.mark.parametrize("fault", ["captcha", "http500", "load"])
def test_a_fault_in_one_band_fails_only_that_run_and_the_batch_continues(app_role_factory, registry, runs, fast_cfg, fault):
    a, b = new_jp(registry), new_jp(registry)
    ka = load_source(app_role_factory, "JP", a.source_id).key_primary
    kb = load_source(app_role_factory, "JP", b.source_id).key_primary
    sa, sb = BandSite(split_items()), BandSite(split_items())
    sa.faults[(100000, 1)] = fault  # セラーAの2つ目の帯の1ページ目だけ失敗し続ける
    router = Router({ka: sa, kb: sb})
    items = run_batch(runs, app_role_factory, router, "JP", [a.source_id, b.source_id], limits=limits(), sleep=lambda s: None)
    ra, rb = items[0].report, items[1].report
    assert ra.outcome == "PARTIAL" and ra.error_code == {"captcha": "CAPTCHA_UNSOLVED", "http500": "SERVER_ERROR", "load": "PAGE_LOAD_FAILED"}[fault]
    assert rb.outcome == "SUCCESS_NONEMPTY"  # 他の出典は継続
    assert sorted(cycle_asins(app_role_factory, rb.cycle_id)) == sorted(x for x, _ in sb.items)
    r = run_row(app_role_factory, ra.run_id)
    assert (r.status, r.cycle_state) == ("FAILED", "OPEN")  # 一時的な失敗: 一巡はOPENのまま、保存済みの位置から続けられる
    saved_before = len(cycle_asins(app_role_factory, ra.cycle_id))
    assert saved_before == 4  # 1つ目の帯は保存済み
    # 原因が直った後は、保存済みの位置（2つ目の帯の1ページ目）から再開
    sa.faults.clear()
    sa.urls.clear()
    c, last = _finish_cycle(runs, app_role_factory, sa, a.source_id)
    assert c.cycle_id == ra.cycle_id
    assert sorted(cycle_asins(app_role_factory, c.cycle_id)) == sorted(x for x, _ in sa.items)
    assert BandSite.parse(sa.urls[0])[0].lo == 100000 and BandSite.parse(sa.urls[0])[1] == 1  # 1つ目の帯を取り直さない


def test_first_page_failure_in_the_first_band_is_failed_and_retries_from_the_first_page(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    site.faults[(0, 1)] = "load"
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code) == ("FAILED", "PAGE_LOAD_FAILED")
    site.faults.clear()
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    assert last.plan == "RETRY_FIRST" and len(cycle_asins(app_role_factory, c.cycle_id)) == 17


def test_a_fault_while_rechecking_a_repeated_page_does_not_split_and_resumes_at_the_same_band(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    n = {"i": 0}
    orig = site.fetch

    def flaky(url):
        b, pg = BandSite.parse(url)
        if b is not None and b.lo == 100000 and b.hi == 200000 and pg == 3:
            n["i"] += 1
            if n["i"] == 2:  # 検知の次の取り直しで失敗
                site.urls.append(url)
                raise PageLoadError("timeout")
        return orig(url)

    site.fetch = flaky
    claim, rep = go(runs, app_role_factory, site, src.source_id)
    assert (rep.outcome, rep.error_code) == ("PARTIAL", "PAGE_LOAD_FAILED")
    assert [b.state for b in bands_of(app_role_factory, claim.cycle_id) if b.lo_units == 100000] == ["ACTIVE"]  # まだ割っていない
    site.fetch = orig
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    assert sorted(cycle_asins(app_role_factory, c.cycle_id)) == sorted(a for a, _ in site.items)
    assert len(leaves(app_role_factory, c.cycle_id)) == 4


def test_a_broken_cycle_restarts_with_its_own_new_bands(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    orig = site.fetch

    def loop(url):
        b, pg = BandSite.parse(url)
        if b is not None and b.lo == 100000 and b.hi == 200000 and pg == 2:  # ページ内容は正しいが、次へが同じURLを指す
            site.urls.append(url)
            return FetchedPage(f'<html><body>{card([asin(99)])}<a class="s-pagination-next" href="{url}">Next</a></body></html>', url, 200)
        return orig(url)

    site.fetch = loop
    c1, r1 = go(runs, app_role_factory, site, src.source_id)
    assert (r1.outcome, r1.error_code) == ("PARTIAL", "REVISITED_URL")
    assert run_row(app_role_factory, c1.run_id).cycle_state == "BROKEN"
    site.fetch = orig
    c2, r2 = go(runs, app_role_factory, site, src.source_id)
    assert r2.plan == "RESTART_BROKEN" and c2.cycle_id != c1.cycle_id
    assert len(bands_of(app_role_factory, c2.cycle_id)) == 5
    assert r2.outcome == "SUCCESS_NONEMPTY"


# ---------------------------------------------------------------- ⑩ 既定OFF・対象外は従来動作

def test_off_by_default_no_band_rows_and_no_price_parameter(app_role_factory, registry, runs, fast_cfg):
    assert RunLimits().price_band is None
    src = new_jp(registry)
    site = BandSite([(asin(i), 1500 * Y) for i in range(6)])
    claim = runs.claim("JP", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9, close_on_repeated_page=True), sleep=lambda s: None)
    assert rep.banded is False and rep.outcome == "SUCCESS_NONEMPTY"
    assert bands_of(app_role_factory, claim.cycle_id) == []
    assert all("p_36" not in u for u in site.urls)
    assert run_row(app_role_factory, claim.run_id).completeness == "COMPLETE"


def test_off_keeps_the_old_depth_limit_behaviour(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite([(asin(i), 1500 * Y) for i in range(20)])  # 帯なしでは2ページ目の後に繰り返す → 従来どおり DEPTH_LIMIT で閉じる
    claim = runs.claim("JP", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=9, close_on_repeated_page=True), sleep=lambda s: None)
    assert (rep.outcome, rep.truncation_reason, rep.pages) == ("SUCCESS_TRUNCATED", "DEPTH_LIMIT", 2)
    assert run_row(app_role_factory, claim.run_id).cycle_state == "CLOSED_NO_CURSOR"
    assert bands_of(app_role_factory, claim.cycle_id) == []


def test_us_and_non_seller_sources_ignore_the_setting(app_role_factory, registry, runs, fast_cfg):
    from tests.integration.test_continuation import Site, mk_pages, new_site

    src, usite = new_site(app_role_factory, registry, mk_pages(2, prefix="B0V"))
    claim = runs.claim("US", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, usite, claim, limits=RunLimits(max_pages=9, close_on_repeated_page=True, price_band=cfg3()), sleep=lambda s: None)
    assert rep.banded is False and rep.outcome == "SUCCESS_NONEMPTY" and all("p_36" not in u for u in usite.urls)
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM acquisition_band WHERE cycle_id = :c"), {"c": claim.cycle_id}).scalar_one() == 0
    jp = new_source(registry, "JP", "KEYWORD", "kw-price-band")
    jsite = Site(mk_pages(2, prefix="B0W"), source_url("JP", load_source(app_role_factory, "JP", jp.source_id)))
    c2 = runs.claim("JP", jp.source_id)
    r2 = run_claimed_source(runs, app_role_factory, jsite, c2, limits=RunLimits(max_pages=9, close_on_repeated_page=True, price_band=cfg3()), sleep=lambda s: None)
    assert r2.banded is False and r2.outcome == "SUCCESS_NONEMPTY"


def test_a_cycle_without_bands_is_finished_the_old_way_even_if_the_setting_is_turned_on_later(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite([(asin(i), 1500 * Y) for i in range(6)])
    c1 = runs.claim("JP", src.source_id)
    r1 = run_claimed_source(runs, app_role_factory, site, c1, limits=RunLimits(max_pages=1, close_on_repeated_page=True), sleep=lambda s: None)
    assert r1.outcome == "SUCCESS_TRUNCATED"
    c2, r2 = go(runs, app_role_factory, site, src.source_id, lim=limits())  # 設定を有効にして続ける
    assert (r2.plan, r2.banded, r2.outcome) == ("CONTINUE", False, "SUCCESS_NONEMPTY")
    assert bands_of(app_role_factory, c2.cycle_id) == []


def test_a_banded_cycle_continues_banded_even_if_the_setting_is_turned_off_later(app_role_factory, registry, runs, fast_cfg):
    src = new_jp(registry)
    site = BandSite(split_items())
    c1, r1 = go(runs, app_role_factory, site, src.source_id, lim=limits(pages=3))
    assert r1.banded
    claim = runs.claim("JP", src.source_id)
    r2 = run_claimed_source(runs, app_role_factory, site, claim, limits=RunLimits(max_pages=99, close_on_repeated_page=True), sleep=lambda s: None)
    assert (r2.banded, r2.outcome) == (True, "SUCCESS_NONEMPTY")
    assert len(cycle_asins(app_role_factory, c1.cycle_id)) == 17


def test_price_band_requires_close_on_repeated_page():
    with pytest.raises(ValueError):
        RunLimits(price_band=cfg3())
    assert RunLimits(close_on_repeated_page=True, price_band=cfg3()).price_band is not None


def test_completed_banded_run_is_a_usable_submission_input(app_role_factory, registry, runs, fast_cfg, tmp_path):
    from tool_submission.planner import build_items

    src = new_jp(registry)
    items = [(asin(i), 1500 * Y) for i in range(8)] + [(asin(50), 500 * Y)]
    claim, rep = go(runs, app_role_factory, BandSite(items), src.source_id)
    assert rep.completeness == "TRUNCATED"
    out, files = build_items(app_role_factory, "JP", claim.run_id, out_dir=tmp_path / "out", direction_override="EXPORT")
    assert [i.run_outcome for i in out] == ["SUCCESS_NONEMPTY"] and out[0].input_is_partial is True  # 範囲限定の注記が付く


# ---------------------------------------------------------------- ⑪ 日次枠（workflow engine の _BudgetFetcher と整合）

def test_every_access_of_a_banded_run_goes_through_the_daily_budget_fetcher(app_role_factory, registry, runs, fast_cfg):
    from workflow.engine import _BudgetFetcher

    src = new_jp(registry)
    site = BandSite(split_items())
    bf = _BudgetFetcher(site, 100, lambda: False)
    claim = runs.claim("JP", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, bf, claim, limits=limits(), sleep=lambda s: None)
    # 保存したページ + 重なり確認 + 繰り返しの検知と取り直し(2回/件) + 空の帯の1ページ目 = 実アクセス = 日次枠に数える数
    assert rep.outcome == "SUCCESS_NONEMPTY" and bf.calls == len(site.urls) == rep.pages + rep.overlap_pages + 2 * rep.repeat_check_pages + rep.band_empty == 11


def test_a_banded_run_stops_as_daily_limit_when_the_budget_is_used_up_and_resumes_without_losing_the_bands(app_role_factory, registry, runs, fast_cfg):
    from workflow.engine import _BudgetFetcher

    src = new_jp(registry)
    site = BandSite(split_items())
    extra_calls = {"n": 0}

    def extra():  # 追加の枠を確保できない（日次上限）
        extra_calls["n"] += 1
        return False

    bf = _BudgetFetcher(site, 7, extra)  # 7アクセス: 帯1(2) + 帯2(2) + 検知と取り直し(2) + 左の子の1ページ目(1)。次で枠切れ（分割の直後）
    claim = runs.claim("JP", src.source_id)
    rep = run_claimed_source(runs, app_role_factory, bf, claim, limits=limits(), sleep=lambda s: None)
    assert rep.outcome == "DAILY_LIMIT" and bf.calls == 7 and extra_calls["n"] == 1
    assert run_row(app_role_factory, claim.run_id).cycle_state == "OPEN"
    c, last = _finish_cycle(runs, app_role_factory, site, src.source_id)
    assert c.cycle_id == claim.cycle_id and len(cycle_asins(app_role_factory, c.cycle_id)) == 17
