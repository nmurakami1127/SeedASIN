"""ページHTMLからのASIN抽出（10桁英数字パターン、selectolax）。

data-asin属性（Amazon商品タイルで広く使われるマーカー）と/dp/XXXXXXXXXX形式のリンクの
両方から候補を探索し、重複排除・正規化（大文字統一）した順序付きリストを返す。
実際のブランド/ランキングページのDOM構造を検証した経験は無いため、これは一般的な
Amazonページの慣習に基づくベストエフォートの実装である。実運用で取りこぼし・誤検出が
見つかれば、このモジュールのセレクタ/正規表現を調整すること（docs/DEVIATIONS.md #18）。

`extract_search_card_asins()`は、実運用で動作確認済みの既存スクリプト
（_reference/amazoncom_seller_asin_collect.py::extract_search_card_asins、
削除済み・docs/DEVIATIONS.md #28参照）のロジックを移植したもの。`extract_asins()`とは
異なり、`div[data-component-type="s-search-result"]`（検索結果カード）の範囲内だけを
見るため、関連検索・スポンサーカルーセル等ページ内の他セクションのASINを拾わない。
`asin_collector.cli collect-brand-page`はこちらを使う。`extract_asins()`は他ページ種別
（将来のranking_page_collector等、カード構造が異なる可能性がある）向けに残してある。
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

_ASIN_RE = re.compile(r"[A-Z0-9]{10}")
_DP_LINK_RE = re.compile(r"/dp/([A-Z0-9]{10})", re.IGNORECASE)
_DP_OR_GP_PRODUCT_LINK_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)


def extract_asins(html: str) -> list[str]:
    tree = HTMLParser(html)
    seen: set[str] = set()
    found: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip().upper()
        if _ASIN_RE.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    for node in tree.css("[data-asin]"):
        value = node.attributes.get("data-asin") or ""
        if value:
            _add(value)

    for node in tree.css("a[href*='/dp/']"):
        href = node.attributes.get("href") or ""
        m = _DP_LINK_RE.search(href)
        if m:
            _add(m.group(1))

    return found


def extract_search_card_asins(html: str) -> list[str]:
    """検索結果カード（`div[data-component-type="s-search-result"]`）範囲内のみから
    ASINを抽出する。カード自身のdata-asin属性を優先し、無ければカード内の
    `/dp/`または`/gp/product/`リンクへフォールバックする（実運用スクリプトの
    extract_search_card_asins()と同じ優先順位、docs/DEVIATIONS.md #28参照）。"""
    tree = HTMLParser(html)
    seen: set[str] = set()
    found: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip().upper()
        if _ASIN_RE.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    for card in tree.css('div[data-component-type="s-search-result"]'):
        card_asin = card.attributes.get("data-asin") or ""
        if card_asin.strip():
            _add(card_asin)
            continue

        link = card.css_first("a[href*='/dp/'], a[href*='/gp/product/']")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        m = _DP_OR_GP_PRODUCT_LINK_RE.search(href)
        if m:
            _add(m.group(1))

    return found
