"""検索結果ページの「次へ」リンク判定（selectolax）。

実運用で動作確認済みの既存スクリプト（_reference/amazoncom_seller_asin_collect.py::
find_next_url、削除済み・docs/DEVIATIONS.md #28参照）のロジックを移植したもの。
元のスクリプトはPlaywrightの`page.locator()`でライブDOMを見ていたが、こちらは
`brand_page_collector.py`の既存設計（`page.content()`で取得した静的HTML文字列を
selectolaxで解析する）に合わせて移植した。判定に使う情報（class名・aria属性・href）は
静的HTMLにも含まれるため、動作に違いは無い。

`a.s-pagination-next`→`aria-label`に"next"を含むリンク、の順に候補を探し、それぞれ
`s-pagination-disabled`クラスまたは`aria-disabled="true"`を持つ場合は無効なリンクとして
スキップする（元のスクリプトと同じ判定基準）。hrefはcurrent_urlを基準に絶対URLへ解決する。
"""
from __future__ import annotations

from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node


def _is_disabled(node: Node) -> bool:
    class_names = (node.attributes.get("class") or "").split()
    if "s-pagination-disabled" in class_names:
        return True
    aria_disabled = (node.attributes.get("aria-disabled") or "").strip().lower()
    return aria_disabled == "true"


def _matches_pagination_next_class(node: Node) -> bool:
    return "s-pagination-next" in (node.attributes.get("class") or "").split()


def _matches_next_aria_label(node: Node) -> bool:
    return "next" in (node.attributes.get("aria-label") or "").lower()


# 元のスクリプトが試す3セレクタ（a.s-pagination-next / aria-label*='next' /
# aria-label*='Next'）は、大文字小文字を無視すれば実質2種類の判定に集約できる。
_CANDIDATE_MATCHERS = (_matches_pagination_next_class, _matches_next_aria_label)


def find_next_url(html: str, current_url: str) -> str | None:
    """次ページへのURLを返す。見つからない・無効化されている場合はNone。"""
    tree = HTMLParser(html)
    anchors = tree.css("a")

    for matcher in _CANDIDATE_MATCHERS:
        for node in anchors:
            if not matcher(node):
                continue
            if _is_disabled(node):
                continue
            href = node.attributes.get("href")
            if href:
                return urljoin(current_url, href)

    return None
