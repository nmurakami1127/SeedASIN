"""asin_collector.check_recommendationsの純粋なロジック・ブラウザ非依存部分の単体テスト。

CheckRecommendationsService._record_no_products()（DB書き込み）の検証は
tests/integration/test_check_recommendations.py参照（brand_page_collector.pyの
既存パターン——DB書き込みの挙動はブラウザ無しで直接呼び出して検証する、を踏襲）。
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path

import pytest

import asin_collector.check_recommendations as cr
from asin_collector.check_recommendations import (
    CheckRecommendationsService,
    UndeterminedSeller,
    classify_search_page,
    read_seller_ids_from_csv,
    storefront_search_url,
)

# 実際のJP「該当商品なし」ページ（seller_id=A26YLYFFSVKSNX）・US「No results」ページで
# 確認したマーカー構造を再現した最小限のHTML（2026-10-07、実機確認）。
_HAS_PRODUCTS_HTML = (
    '<div data-component-type="s-search-result" data-asin="B0TESTASN1">item</div>'
)
_NO_RESULTS_HTML = (
    '<div data-csa-c-content-id="messaging-messages-no-results">'
    "検索クエリに一致する結果はありません。</div>"
    # 実ページ同様、検索結果カードの外側にスポンサー商品のdata-asinが存在しても
    # 0件判定に影響しないことを確認する。
    '<div class="sponsored-slot"><div data-asin="B0SPONSOR01">sponsored</div></div>'
)
_UNEXPECTED_STRUCTURE_HTML = "<html><body>想定外のページ構造</body></html>"
_CAPTCHA_HTML = "<p>Enter the characters you see below</p>"
_SERVER_ERROR_HTML = '<a href="/ref=cs_503_logo"><img alt="Amazon.com"></a>'


class TestStorefrontSearchUrl:
    def test_jp_url_format(self):
        assert storefront_search_url("JP", "A26YLYFFSVKSNX") == "https://www.amazon.co.jp/s?me=A26YLYFFSVKSNX"

    def test_us_url_format(self):
        assert (
            storefront_search_url("US", "A1EXAMPLE")
            == "https://www.amazon.com/s?me=A1EXAMPLE&marketplaceID=ATVPDKIKX0DER"
        )

    def test_url_encodes_special_characters_in_seller_id(self):
        """seller_idは自由入力のブランド名等（括弧・日本語を含み得る、
        tool_intake/log_ingest.py参照）でもあり得るため、URLとして安全に
        エンコードすること。"""
        url = storefront_search_url("JP", "アスカモデル(ASUKAMODEL)")
        assert url.split("me=", 1)[1] == "%E3%82%A2%E3%82%B9%E3%82%AB%E3%83%A2%E3%83%87%E3%83%AB%28ASUKAMODEL%29"

    def test_invalid_marketplace_raises(self):
        with pytest.raises(ValueError):
            storefront_search_url("UK", "A1EXAMPLE")


class TestClassifySearchPage:
    def test_has_products_when_search_result_cards_present(self):
        assert classify_search_page(_HAS_PRODUCTS_HTML) == "HAS_PRODUCTS"

    def test_no_products_when_no_results_marker_present_and_no_cards(self):
        """「該当商品なし」ページにスポンサー商品のdata-asinが含まれていても、
        検索結果カード（s-search-result）の外側であれば0件と判定すること
        （ユーザー指摘の懸念、2026-10-07実機確認）。"""
        assert classify_search_page(_NO_RESULTS_HTML) == "NO_PRODUCTS"

    def test_undetermined_when_neither_cards_nor_marker_present(self):
        """カードが0件でも「検索結果0件」ウィジェットが見つからない（想定外の構造）
        場合は、0件と断定せず判定不能にすること（ユーザー指定）。"""
        assert classify_search_page(_UNEXPECTED_STRUCTURE_HTML) == "UNDETERMINED"


class TestReadSellerIdsFromCsv:
    def test_reads_seller_id_column(self, tmp_path: Path):
        path = tmp_path / "recommend.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["seller_id", "最終取得日", "保留記録日"])
            writer.writerow(["A1EXAMPLE", "2026-02-10", ""])
            writer.writerow(["A2EXAMPLE", "", "2026-09-17"])

        assert read_seller_ids_from_csv(path) == ["A1EXAMPLE", "A2EXAMPLE"]

    def test_skips_blank_seller_id_rows(self, tmp_path: Path):
        path = tmp_path / "recommend.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["seller_id", "最終取得日"])
            writer.writerow(["A1EXAMPLE", "2026-02-10"])
            writer.writerow(["", "2026-02-10"])

        assert read_seller_ids_from_csv(path) == ["A1EXAMPLE"]

    def test_reads_cp932_encoded_file(self, tmp_path: Path):
        """Excelで開いて編集後、日本語版Windowsの既定でcp932保存されたCSVでも
        読み取れること（CLAUDE.md技術スタックのエンコーディング自動判定要件）。"""
        path = tmp_path / "recommend_cp932.csv"
        content = "seller_id,保留理由\nA1EXAMPLE,同業者利用\n"
        path.write_bytes(content.encode("cp932"))

        assert read_seller_ids_from_csv(path) == ["A1EXAMPLE"]

    def test_missing_seller_id_column_raises(self, tmp_path: Path):
        path = tmp_path / "bad.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["not_seller_id"])
            writer.writerow(["A1EXAMPLE"])

        with pytest.raises(ValueError):
            read_seller_ids_from_csv(path)


class _StatefulFakePage:
    """tests/unit/test_brand_page_collector_retry.pyと同じパターン（goto呼び出し回数で
    返すHTMLを切り替える）。"""

    def __init__(self, contents_sequence: list[str]) -> None:
        self._contents = contents_sequence
        self.goto_calls = 0
        self.goto_exception: Exception | None = None

    def goto(self, url: str) -> None:
        self.goto_calls += 1
        if self.goto_exception is not None:
            raise self.goto_exception

    def content(self) -> str:
        idx = min(self.goto_calls - 1, len(self._contents) - 1)
        return self._contents[idx]


class TestCheckOne:
    def test_has_products(self):
        page = _StatefulFakePage([_HAS_PRODUCTS_HTML])
        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")
        assert status == "HAS_PRODUCTS"
        assert reason is None
        assert page.goto_calls == 1

    def test_no_products(self):
        page = _StatefulFakePage([_NO_RESULTS_HTML])
        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")
        assert status == "NO_PRODUCTS"
        assert reason is None

    def test_undetermined_on_unexpected_structure(self):
        page = _StatefulFakePage([_UNEXPECTED_STRUCTURE_HTML])
        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")
        assert status == "UNDETERMINED"
        assert reason == "UNEXPECTED_STRUCTURE"

    def test_undetermined_on_captcha_without_blocking_wait(self):
        """brand_page_collector.pyと異なり、CAPTCHA検知時に人の対話待ち
        （wait_for_human_to_solve_captcha）をしないこと——即座にUNDETERMINEDを返し、
        goto()は1回のみであること（ユーザー確認済み設計、docs/DEVIATIONS.md #53参照）。"""
        page = _StatefulFakePage([_CAPTCHA_HTML])
        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")
        assert status == "UNDETERMINED"
        assert reason == "CAPTCHA"
        assert page.goto_calls == 1

    def test_retries_on_server_error_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(cr.time, "sleep", lambda seconds: None)
        page = _StatefulFakePage([_SERVER_ERROR_HTML, _HAS_PRODUCTS_HTML])

        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")

        assert status == "HAS_PRODUCTS"
        assert page.goto_calls == 2

    def test_undetermined_after_server_error_retries_exhausted(self, monkeypatch):
        monkeypatch.setattr(cr.time, "sleep", lambda seconds: None)
        page = _StatefulFakePage([_SERVER_ERROR_HTML])  # 常にエラー

        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")

        assert status == "UNDETERMINED"
        assert reason == "SERVER_ERROR"
        assert page.goto_calls == cr._SERVER_ERROR_MAX_RETRIES + 1

    def test_undetermined_when_page_load_fails(self):
        page = _StatefulFakePage([])
        page.goto_exception = RuntimeError("net::ERR_CONNECTION_RESET")

        status, reason = CheckRecommendationsService()._check_one(page, "JP", "A1EXAMPLE")

        assert status == "UNDETERMINED"
        assert reason == "PAGE_LOAD_FAILED"


class _FakeContext:
    def __init__(self, page) -> None:
        self._page = page

    def new_page(self):
        return self._page


@contextmanager
def _fake_open_browser_cm(context):
    yield context


def test_check_sellers_aggregates_outcomes_and_records_only_no_products(monkeypatch):
    """check_sellers()の集計ロジック: HAS_PRODUCTS/NO_PRODUCTS/UNDETERMINEDを正しく
    カウントし、NO_PRODUCTSのみ_record_no_products()を呼ぶこと。ブラウザ・DBいずれも
    実体は使わない（_check_one/_record_no_products/open_browserをモック）。"""
    outcomes = {
        "SELLER_HAS": ("HAS_PRODUCTS", None),
        "SELLER_NONE": ("NO_PRODUCTS", None),
        "SELLER_CAPTCHA": ("UNDETERMINED", "CAPTCHA"),
    }
    recorded: list[str] = []

    monkeypatch.setattr(
        cr, "open_browser", lambda marketplace: _fake_open_browser_cm(_FakeContext(object()))
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_check_one",
        lambda self, page, marketplace, seller_id: outcomes[seller_id],
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_record_no_products",
        lambda self, session_factory, marketplace, seller_id: (recorded.append(seller_id), "NEW")[1],
    )

    summary = CheckRecommendationsService().check_sellers(
        None, "JP", ["SELLER_HAS", "SELLER_NONE", "SELLER_CAPTCHA"]
    )

    assert summary.has_products == 1
    assert summary.no_products_new == 1
    assert summary.no_products_upgraded == 0
    assert summary.no_products_already_excluded == 0
    assert summary.undetermined == [UndeterminedSeller("SELLER_CAPTCHA", "CAPTCHA")]
    assert recorded == ["SELLER_NONE"]
    assert summary.processed_count == 3
    assert summary.aborted is False
    assert summary.remaining_seller_ids == []


def test_check_sellers_counts_upgraded_and_already_excluded_separately(monkeypatch):
    """_record_no_products()の戻り値（"NEW"/"UPGRADED"/"ALREADY_EXCLUDED"）に応じて、
    サマリの3つのカウンタへ正しく振り分けること（ユーザー依頼、docs/DEVIATIONS.md #54
    参照）。"""
    outcomes = {
        "SELLER_NEW": ("NO_PRODUCTS", None),
        "SELLER_UPGRADED": ("NO_PRODUCTS", None),
        "SELLER_ALREADY": ("NO_PRODUCTS", None),
    }
    record_outcomes = {"SELLER_NEW": "NEW", "SELLER_UPGRADED": "UPGRADED", "SELLER_ALREADY": "ALREADY_EXCLUDED"}

    monkeypatch.setattr(
        cr, "open_browser", lambda marketplace: _fake_open_browser_cm(_FakeContext(object()))
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_check_one",
        lambda self, page, marketplace, seller_id: outcomes[seller_id],
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_record_no_products",
        lambda self, session_factory, marketplace, seller_id: record_outcomes[seller_id],
    )

    summary = CheckRecommendationsService().check_sellers(
        None, "JP", ["SELLER_NEW", "SELLER_UPGRADED", "SELLER_ALREADY"]
    )

    assert summary.no_products_new == 1
    assert summary.no_products_upgraded == 1
    assert summary.no_products_already_excluded == 1


def test_check_sellers_aborts_after_consecutive_undetermined_threshold(monkeypatch):
    """判定不能がmax_consecutive_undetermined件連続したら、残りのseller_idを処理せず
    中断すること。処理済み件数・未処理のseller_id一覧を正しく返すこと（ユーザー依頼、
    docs/DEVIATIONS.md #54参照）。"""
    monkeypatch.setattr(
        cr, "open_browser", lambda marketplace: _fake_open_browser_cm(_FakeContext(object()))
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_check_one",
        lambda self, page, marketplace, seller_id: ("UNDETERMINED", "CAPTCHA"),
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_record_no_products",
        lambda self, session_factory, marketplace, seller_id: pytest.fail("呼ばれないはず"),
    )

    seller_ids = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    summary = CheckRecommendationsService().check_sellers(None, "JP", seller_ids, max_consecutive_undetermined=3)

    assert summary.aborted is True
    assert summary.processed_count == 3
    assert len(summary.undetermined) == 3
    assert summary.remaining_seller_ids == ["S4", "S5", "S6", "S7"]


def test_check_sellers_resets_consecutive_undetermined_counter_on_success(monkeypatch):
    """HAS_PRODUCTS/NO_PRODUCTSが1件でも挟まれば、連続判定不能のカウントはリセットされ、
    閾値未満のまま最後まで処理が続くこと。"""
    sequence = {
        "S1": ("UNDETERMINED", "CAPTCHA"),
        "S2": ("UNDETERMINED", "CAPTCHA"),
        "S3": ("HAS_PRODUCTS", None),
        "S4": ("UNDETERMINED", "CAPTCHA"),
        "S5": ("UNDETERMINED", "CAPTCHA"),
    }
    monkeypatch.setattr(
        cr, "open_browser", lambda marketplace: _fake_open_browser_cm(_FakeContext(object()))
    )
    monkeypatch.setattr(
        CheckRecommendationsService,
        "_check_one",
        lambda self, page, marketplace, seller_id: sequence[seller_id],
    )

    summary = CheckRecommendationsService().check_sellers(
        None, "JP", list(sequence.keys()), max_consecutive_undetermined=3
    )

    assert summary.aborted is False
    assert summary.processed_count == 5
    assert len(summary.undetermined) == 4
