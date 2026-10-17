"""recommendが出力したCSV（seller_performance.cli recommend / 対話式メニューcli.py
メニュー5、docs/DEVIATIONS.md #53参照）を入力に、各セラーのAmazonストアフロント検索
ページを巡回し、出品商品が0件と確認できたセラーをstorefront_blocked_sellerへ
PERMANENTLY_EXCLUDEDとして記録する（ユーザー依頼、2026-10-07）。

ストアフロント検索URL（ユーザーが実際にブラウザで確認し確定した形式）:
- JP: https://www.amazon.co.jp/s?me=<seller_id>
- US: https://www.amazon.com/s?me=<seller_id>&marketplaceID=ATVPDKIKX0DER

**商品0件の判定方法**: asin_collector.asin_extractor.extract_search_card_asins()
（collect-brand-pageと同じ関数。div[data-component-type="s-search-result"]範囲内
のみを見るため、スポンサー枠・おすすめ商品は含まれない）で抽出したASINが0件、かつ
Amazon自身の「検索結果0件」ウィジェット（data-csa-c-content-id=
"messaging-messages-no-results"）が存在する、の両方を満たした場合のみ「商品なし」
と判定する。ASIN件数だけで判定しない理由: 「該当商品なし」のページにもスポンサー
商品・おすすめ商品が表示されることがあり、これらのdata-asinを拾ってしまうと誤って
「商品あり」と判定し得る（ユーザー指摘）。逆に単に「検索結果0件ウィジェットの有無」
だけで判定しない理由: 想定外のページ構造でウィジェットの文言・マーカーが変わった
場合に、実際には商品があるのに誤って0件と判定するリスクを避けるため、
extract_search_card_asins()による実際のカード検出と組み合わせる。

実機確認（2026-10-07、ユーザー提供の実セラーIDで検証）:
- JP seller_id=A26YLYFFSVKSNX（「該当商品なし」）: extract_search_card_asins()は
  0件、上記ウィジェットは存在 → NO_PRODUCTS。
- JP seller_id=A3SQM3QBSWZXTB（商品あり）: extract_search_card_asins()は16件、
  ウィジェットは存在しない → HAS_PRODUCTS。
- US側もダミーseller_idの「No results for your search query.」ページで同じ
  ウィジェットマーカーが存在することを確認済み（Amazon内部のwidget ID文字列のため
  表示言語に依存しない）。

**カードが0件でもウィジェットが見つからない場合（想定外の構造）、CAPTCHA検知時、
ページ読み込み失敗、一時的な500/503エラー（再試行後も解消しない場合）は、いずれも
「商品なし」と断定せず「判定不能」として一覧に残し、storefront_blocked_sellerへは
記録しない**（誤ってPERMANENTLY_EXCLUDEDに記録しないため、ユーザー指定）。

CAPTCHA検知時の扱いはasin_collector.brand_page_collector.pyと異なり、人の対話待ち
（wait_for_human_to_solve_captcha）は行わない——recommendのCSVには多数のセラーが
含まれ得るため（ユーザーからの実装前確認で、known_seller_masterへの候補拡大後は
1marketplaceあたり数百件規模）、1件ごとに対話待ちでブロッキングする設計は現実的で
ないと判断した（ユーザー確認済み）。CAPTCHAが起きたセラーは「判定不能」として
記録し、必要なら後日ブラウザで手動確認するか、再実行することを想定する。

2026-10-07追加（ユーザー依頼、docs/DEVIATIONS.md #54参照）、以下2点:

1. NO_PRODUCTSと判定したセラーが、すでにstorefront_blocked_sellerに
   COMPETITOR_SAME_SERVICE・RETRY_LATERで記録されている場合、PERMANENTLY_EXCLUDED
   へ更新する（手動で保留にしたセラーがrecommendの一覧に残り続ける問題の修正）。
   実装はseller_ledger.storefront_blocked.StorefrontBlockedSellerService.
   mark_or_escalate_to_permanently_excluded()に委譲（同モジュールdocstring
   「4つ目の記録経路」参照）。新規記録・既存保留からの更新・既にPERMANENTLY_EXCLUDED
   （変更なし）の3通りを区別してサマリに集計する。
2. 判定不能（UNDETERMINED）が連続max_consecutive_undetermined件（既定値5、
   check-recommendations --max-consecutive-undeterminedで変更可）続いたら、残りの
   seller_idを処理せずに中断する。CAPTCHAやページ構造の変化など、1件の判定不能が
   もはや個別の偶発的失敗ではなく、ブラウザ・ネットワーク・Amazon側の状態異常を
   示している可能性が高いと判断されるため（1件ずつのエラー分離とは別の、バッチ単位
   での早期中断。CLAUDE.md 6節のエラー分離は「1件の失敗で他のレコードを止めない」
   ことを求めるが、同種の失敗が連続する場合はこの限りではないと判断した）。中断時は
   処理済み件数と未処理のseller_id一覧を返す——storefront_blocked_sellerへの書き込みは
   既にべき等（mark_or_escalate_to_permanently_excluded()は再実行しても安全）のため、
   同じCSVファイルでそのまま再実行できる。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from playwright.sync_api import Page

from asin_collector.asin_extractor import extract_search_card_asins
from asin_collector.browser_session import is_captcha_page, is_server_error_page, open_browser
from db.session import SessionFactory
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from tool_intake.csv_utils import column_index, read_csv_rows

# Amazon内部のウィジェットID（表示言語に依存しない、JP/US双方の実ページで確認済み）。
_NO_RESULTS_MARKER = 'data-csa-c-content-id="messaging-messages-no-results"'
NO_RESULTS_MARKER = _NO_RESULTS_MARKER  # 他モジュール(source_runner)から参照する公開名

# brand_page_collector.pyと同じ値（一時的な500/503エラーからの自動リトライ、
# docs/DEVIATIONS.md #19参照）。
_SERVER_ERROR_MAX_RETRIES = 2
_SERVER_ERROR_RETRY_WAIT_SECONDS = 5

# 判定不能が連続した場合の中断閾値の既定値（ユーザー依頼、docs/DEVIATIONS.md #54参照）。
_DEFAULT_MAX_CONSECUTIVE_UNDETERMINED = 5

CheckStatus = Literal["HAS_PRODUCTS", "NO_PRODUCTS", "UNDETERMINED"]


def storefront_search_url(marketplace: str, seller_id: str) -> str:
    """ユーザーが実際にブラウザで確認し確定したURL形式（2026-10-07）。"""
    encoded = quote(seller_id, safe="")
    if marketplace == "JP":
        return f"https://www.amazon.co.jp/s?me={encoded}"
    if marketplace == "US":
        return f"https://www.amazon.com/s?me={encoded}&marketplaceID=ATVPDKIKX0DER"
    raise ValueError(f"marketplace must be 'US' or 'JP', got: {marketplace!r}")


def classify_search_page(html: str) -> CheckStatus:
    """ページHTMLから商品の有無を判定する（ブラウザ非依存、単体テスト可能）。
    カードが0件でも「検索結果0件」ウィジェットが見つからない場合は、想定外の
    ページ構造とみなしUNDETERMINEDにする（誤って0件と断定しないため、ユーザー指定）。
    """
    if extract_search_card_asins(html):
        return "HAS_PRODUCTS"
    if _NO_RESULTS_MARKER in html:
        return "NO_PRODUCTS"
    return "UNDETERMINED"


def read_seller_ids_from_csv(path: Path) -> list[str]:
    """seller_performance.cli recommendが出力したCSV（またはExcelで開いて編集後、
    日本語版Windowsの既定でcp932保存されたもの）からseller_id列を読み取る
    （utf-8-sig→cp932→utf-8の自動判定、CLAUDE.md技術スタック参照。
    tool_intake.csv_utilsをそのまま再利用する）。空行・seller_id空欄の行は無視する。
    """
    header, data_rows = read_csv_rows(path)
    idx = column_index(header, "seller_id")
    return [row[idx] for row in data_rows if row and row[idx].strip()]


@dataclass
class UndeterminedSeller:
    seller_id: str
    reason: str  # 'CAPTCHA' / 'SERVER_ERROR' / 'PAGE_LOAD_FAILED' / 'UNEXPECTED_STRUCTURE'


@dataclass
class CheckRecommendationsSummary:
    has_products: int = 0
    no_products_new: int = 0
    no_products_upgraded: int = 0
    no_products_already_excluded: int = 0
    undetermined: list[UndeterminedSeller] = field(default_factory=list)
    processed_count: int = 0
    aborted: bool = False
    remaining_seller_ids: list[str] = field(default_factory=list)


class CheckRecommendationsService:
    def check_sellers(
        self,
        session_factory: SessionFactory,
        marketplace: str,
        seller_ids: list[str],
        max_consecutive_undetermined: int = _DEFAULT_MAX_CONSECUTIVE_UNDETERMINED,
    ) -> CheckRecommendationsSummary:
        """1ブラウザセッションでseller_idを順に巡回する。1件の判定失敗（判定不能）が
        他のセラーの処理を止めないこと自体はCLAUDE.md 6節のエラー分離の精神を踏襲するが、
        判定不能がmax_consecutive_undetermined件連続した場合はバッチ全体を中断する
        （2026-10-07追加、モジュールdocstring参照）。HAS_PRODUCTS・NO_PRODUCTSのいずれかが
        1件でも判定できれば連続回数はリセットされる。
        """
        summary = CheckRecommendationsSummary()
        consecutive_undetermined = 0
        with open_browser(marketplace) as context:
            page = context.new_page()
            for index, seller_id in enumerate(seller_ids):
                status, reason = self._check_one(page, marketplace, seller_id)
                summary.processed_count += 1
                if status == "HAS_PRODUCTS":
                    summary.has_products += 1
                    consecutive_undetermined = 0
                elif status == "NO_PRODUCTS":
                    outcome = self._record_no_products(session_factory, marketplace, seller_id)
                    if outcome == "NEW":
                        summary.no_products_new += 1
                    elif outcome == "UPGRADED":
                        summary.no_products_upgraded += 1
                    else:
                        summary.no_products_already_excluded += 1
                    consecutive_undetermined = 0
                else:
                    summary.undetermined.append(UndeterminedSeller(seller_id, reason or "UNEXPECTED_STRUCTURE"))
                    consecutive_undetermined += 1
                    if consecutive_undetermined >= max_consecutive_undetermined:
                        summary.aborted = True
                        summary.remaining_seller_ids = list(seller_ids[index + 1 :])
                        break
        return summary

    def _check_one(self, page: Page, marketplace: str, seller_id: str) -> tuple[CheckStatus, str | None]:
        url = storefront_search_url(marketplace, seller_id)
        for attempt in range(1, _SERVER_ERROR_MAX_RETRIES + 2):
            try:
                page.goto(url)
            except Exception:
                return "UNDETERMINED", "PAGE_LOAD_FAILED"

            if is_captcha_page(page):
                # brand_page_collector.pyと異なり対話待ちはしない（モジュールdocstring参照）。
                return "UNDETERMINED", "CAPTCHA"

            if is_server_error_page(page):
                if attempt <= _SERVER_ERROR_MAX_RETRIES:
                    time.sleep(_SERVER_ERROR_RETRY_WAIT_SECONDS)
                    continue
                return "UNDETERMINED", "SERVER_ERROR"

            status = classify_search_page(page.content())
            return status, (None if status != "UNDETERMINED" else "UNEXPECTED_STRUCTURE")

        return "UNDETERMINED", "SERVER_ERROR"  # pragma: no cover - ループは必ずreturnで抜ける

    def _record_no_products(self, session_factory: SessionFactory, marketplace: str, seller_id: str) -> str:
        """戻り値はEscalateResult.status（"NEW"/"UPGRADED"/"ALREADY_EXCLUDED"）。
        既存のCOMPETITOR_SAME_SERVICE・RETRY_LATER記録をPERMANENTLY_EXCLUDEDへ更新する
        ロジックはseller_ledger.storefront_blocked.mark_or_escalate_to_permanently_excluded()
        に集約してある（docs/DEVIATIONS.md #54参照）。"""
        with session_factory.get_session(marketplace) as conn, conn.begin():
            result = StorefrontBlockedSellerService().mark_or_escalate_to_permanently_excluded(
                conn,
                seller_id,
                new_record_note="check-recommendationsでストアフロント検索結果0件を確認（自動記録）",
            )
        return result.status
