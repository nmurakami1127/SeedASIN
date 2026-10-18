"""商品詳細ページ（/dp/<ASIN>）からの出品者・ブランド候補の抽出と、ページの取得結果の区別。

**抽出（純粋関数）とブラウザ操作は分離している。** `extract_product_page(html)` はHTML文字列だけを見る
ので疑似ページで検証できる。取得は `PageFetcher`（asin_collector.source_runner）の背後にある。

**実ページの構造は未検証**（この環境では商品詳細ページを開いていない）。下のセレクタは一般に知られた
Amazonのマークアップ（bylineInfo / po-brand / merchant-info / aod-offer 等）に基づくベストエフォートで、
疑似ページ（tests/unit/test_product_page.py）で検証したにすぎない。実ページで外れていれば、このファイルの
セレクタだけを調整する。構造を認識できないときは候補を推測せず `UNCONFIRMED` / 候補0件にする。

**抽出元と範囲を区別して記録する**（`seller_scope`）:
- `VISIBLE_OFFERS` : 出品一覧パネル（aod）に表示されていた出品者。表示範囲であって「全出品者」とは確認できない
- `BUYBOX_ONLY`    : 購入ボックス（カート）の出品者1件だけ。**これだけで「全セラー取得」とは扱わない**
- `NONE`           : 出品者を抽出できなかった
商品ページ内の他の領域（おすすめ・広告・関連商品）のリンクは拾わない（認識した領域の内側だけを見る）。

**販売元リンク・販売元表示・配送元表示を区別する**: 候補にするのは`seller=`付きのリンクだけ（販売元リンク）。配送元の表示内
（fulfillerInfo / shipsFrom 等の目印）にあるものは`shippers`に分けて出典にしない（FBAは配送元がAmazonなだけ）。販売元の表示に
リンク（ID）が無いものは、IDが無いので登録できない。役割の目印が見つからないリンクは`UNSPECIFIED`として扱い、決めつけない。

**ブランド名・ブランドストアは混同しない**:
- ブランドストア(BRAND_STORE): bylineInfoのリンクが `/stores/` を指すとき、そのURL
- ブランド名(BRAND_NAME): bylineInfoのリンクが `rh=...p_89:<名前>`（ブランド絞込）を指すとき、または商品詳細表の
  ブランド行(po-brand)の値。ストアのリンク文言（「Visit the X Store」）からブランド名は導かない

**ページの状態**（存在未確認・該当なし・読込失敗を区別）:
- `FOUND`       商品ページと確認できる目印がある
- `NOT_FOUND`   HTTP 404 のとき**だけ**（文言だけでは断定しない）
- `UNCONFIRMED` 取得できたが商品ページと確認できない（構造不明）
読込失敗・5xx・CAPTCHAは `fetch_product_page` が別の種別で返す。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from selectolax.parser import HTMLParser

from asin_collector.browser_session import html_is_captcha, html_is_server_error
from asin_collector.source_runner import DOMAIN, AccessBudgetExhausted, CaptchaNotSolved, FetchedPage, PageFetcher, PageLoadError

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_SELLER_PARAM = re.compile(r"[?&]seller=([A-Za-z0-9]{8,20})(?:[&#]|$)")
_FOUND_MARKERS = ('id="productTitle"', 'id="dp-container"', 'id="ppd"', 'id="centerCol"')

# 購入ボックス領域 / 出品一覧パネル領域（実ページ未検証。ここだけを見る）
_BUYBOX_SELECTORS = ("#merchant-info", "#tabular-buybox", "#sellerProfileTriggerId", "#offer-display-features")
_OFFER_SELECTORS = ("#aod-offer-list", "#aod-pinned-offer")


@dataclass(frozen=True)
class SellerRef:
    seller_id: str
    name: str | None
    region: str  # 'VISIBLE_OFFERS' / 'BUYBOX'
    role: str = "UNSPECIFIED"  # 'SOLD_BY'（販売元の表示内のリンク）/ 'SHIPS_FROM'（配送元の表示内）/ 'UNSPECIFIED'（seller=付きのリンクだが役割の目印なし）


@dataclass(frozen=True)
class BrandRef:
    source_type: str  # 'BRAND_NAME' / 'BRAND_STORE'
    key: str  # ブランド名 / ストアURL
    basis: str  # 'STORE_LINK' / 'REFINEMENT_LINK' / 'DETAIL_TABLE'


@dataclass
class ProductPageResult:
    status: str  # FOUND / NOT_FOUND / UNCONFIRMED
    sellers: list[SellerRef] = field(default_factory=list)
    brands: list[BrandRef] = field(default_factory=list)
    seller_scope: str = "NONE"  # VISIBLE_OFFERS / BUYBOX_ONLY / NONE
    notes: list[str] = field(default_factory=list)
    shippers: list[SellerRef] = field(default_factory=list)  # 配送元の表示内にあった識別子（販売元ではないので出典にしない。報告用）


def product_url(market: str, asin: str) -> str:
    if market not in DOMAIN:
        raise ValueError(f"unknown market: {market!r}")
    a = asin.strip().upper()
    if not ASIN_RE.match(a):
        raise ValueError(f"ASINの形式が不正です: {asin!r}")
    return f"https://{DOMAIN[market]}/dp/{a}"


# 販売元／配送元の表示を見分ける目印（実ページ未検証。外れていれば役割は UNSPECIFIED になり、販売元とも配送元とも決めつけない）
_SHIPS_MARKERS = ("fulfillerinfo", "shipsfrom", "ships-from", "ships_from")
_SOLD_MARKERS = ("merchantinfofeature", "sellerprofiletrigger", "soldby", "sold-by", "sold_by")


def _role(a) -> str:
    node, depth = a, 0
    while node is not None and depth < 8:
        marks = f"{node.attributes.get('id') or ''} {node.attributes.get('class') or ''}".lower()
        if any(m in marks for m in _SHIPS_MARKERS):
            return "SHIPS_FROM"
        if any(m in marks for m in _SOLD_MARKERS):
            return "SOLD_BY"
        node, depth = node.parent, depth + 1
    return "UNSPECIFIED"


def _seller_links(tree: HTMLParser, selectors: tuple[str, ...], region: str) -> list[SellerRef]:
    out: list[SellerRef] = []
    for sel in selectors:
        for container in tree.css(sel):
            anchors = [container] if container.tag == "a" else container.css("a")
            for a in anchors:
                m = _SELLER_PARAM.search(a.attributes.get("href") or "")
                if m:
                    name = (a.text(separator=" ").strip() or None)
                    out.append(SellerRef(m.group(1).upper(), name, region, _role(a)))
    return out


def _brands(tree: HTMLParser) -> list[BrandRef]:
    out: list[BrandRef] = []
    byline = tree.css_first("#bylineInfo")
    href = (byline.attributes.get("href") or "") if byline is not None else ""
    if byline is not None and href:
        path = urlparse(href).path
        if "/stores/" in path:
            out.append(BrandRef("BRAND_STORE", href, "STORE_LINK"))
        else:
            for rh in parse_qs(urlparse(href).query).get("rh", []):
                for part in unquote(rh).split(","):
                    if part.startswith("p_89:"):
                        name = part[len("p_89:"):].replace("+", " ").strip()
                        if name and "|" not in name:
                            out.append(BrandRef("BRAND_NAME", name, "REFINEMENT_LINK"))
    row = tree.css_first("tr.po-brand .po-break-word, .po-brand .po-break-word")
    if row is not None and row.text().strip():
        out.append(BrandRef("BRAND_NAME", row.text().strip(), "DETAIL_TABLE"))
    seen, uniq = set(), []
    for b in out:
        k = (b.source_type, b.key.casefold())
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    return uniq


def extract_product_page(html: str, status: int | None = None) -> ProductPageResult:
    """HTMLから状態・出品者・ブランドを取り出す（純粋関数）。"""
    if status == 404:
        return ProductPageResult("NOT_FOUND", notes=["HTTP 404"])
    if not any(m in html for m in _FOUND_MARKERS):
        return ProductPageResult("UNCONFIRMED", notes=["商品ページと確認できる目印がありません"])
    tree = HTMLParser(html)
    offers = _seller_links(tree, _OFFER_SELECTORS, "VISIBLE_OFFERS")
    buybox = _seller_links(tree, _BUYBOX_SELECTORS, "BUYBOX")
    sellers: list[SellerRef] = []
    shippers: list[SellerRef] = []
    seen: set[str] = set()
    for s in buybox + offers:  # 購入ボックスの出品者を先に、重複は1件
        if s.role == "SHIPS_FROM":  # 配送元の表示。販売元ではないので出典の候補にしない
            shippers.append(s)
        elif s.seller_id not in seen:
            seen.add(s.seller_id)
            sellers.append(s)
    scope = "VISIBLE_OFFERS" if any(s.region == "VISIBLE_OFFERS" for s in sellers) else ("BUYBOX_ONLY" if sellers else "NONE")
    notes = []
    if scope == "BUYBOX_ONLY":
        notes.append("購入ボックスの出品者のみ（全出品者の取得とは扱わない）")
    brands = _brands(tree)
    if not brands:  # ブランドを認識できなかった理由を切り分けられるよう、目印の有無だけを残す（HTMLは保存しない）
        byline = tree.css_first("#bylineInfo")
        notes.append("ブランド未抽出: bylineInfo=" + ("なし" if byline is None else f"あり(href={'あり' if byline.attributes.get('href') else 'なし'})")
                     + ", po-brand=" + ("あり" if tree.css_first(".po-brand") is not None else "なし"))
    return ProductPageResult("FOUND", sellers=sellers, brands=brands, seller_scope=scope, notes=notes, shippers=shippers)


# ---------------------------------------------------------------------- 取得（ブラウザ非依存）

@dataclass
class PageOutcome:
    kind: str  # FOUND / NOT_FOUND / UNCONFIRMED / LOAD_FAILED / SERVER_ERROR / CAPTCHA_UNSOLVED / DAILY_LIMIT（日次枠の不足。ページを開いていない）
    result: ProductPageResult | None = None
    detail: str | None = None
    url: str | None = None


def fetch_product_page(
    fetcher: PageFetcher, market: str, asin: str, *, server_error_retries: int = 2, server_error_wait: float = 5.0,
    sleep: Callable[[float], None] = time.sleep, on_captcha_start: Callable[[], None] | None = None,
    on_captcha_end: Callable[[], None] | None = None,
) -> PageOutcome:
    """商品詳細ページを1ページ取得して分類する。CAPTCHAは回避せず、人の対応を1回だけ待って1回だけ再取得する。
    5xxは数回まで自動リトライ。いずれも同じ対象を繰り返し試すことはしない。"""
    url = product_url(market, asin)
    captcha_rounds, attempt = 0, 0
    while True:
        try:
            page: FetchedPage = fetcher.fetch(url)
        except AccessBudgetExhausted as exc:  # 日次枠が尽きた: ページの失敗ではない（調査の失敗・冷却・自動停止に数えない）
            return PageOutcome("DAILY_LIMIT", detail=str(exc)[:300], url=url)
        except PageLoadError as exc:
            return PageOutcome("LOAD_FAILED", detail=str(exc)[:300], url=url)
        if html_is_captcha(page.html):
            captcha_rounds += 1
            if captcha_rounds > 1:
                return PageOutcome("CAPTCHA_UNSOLVED", detail="CAPTCHAが解消しませんでした", url=url)
            if on_captcha_start:
                on_captcha_start()  # DB上でCAPTCHA待機を見える化し、leaseを長めに延ばす
            try:
                fetcher.wait_for_captcha_solved()
            except CaptchaNotSolved as exc:
                return PageOutcome("CAPTCHA_UNSOLVED", detail=str(exc)[:300], url=url)
            if on_captcha_end:
                on_captcha_end()
            continue
        if (page.status is not None and page.status >= 500) or html_is_server_error(page.html):
            attempt += 1
            if attempt <= server_error_retries:
                sleep(server_error_wait)
                continue
            return PageOutcome("SERVER_ERROR", detail=f"5xx/エラーページが{attempt}回続きました（status={page.status}）", url=url)
        res = extract_product_page(page.html, page.status)
        return PageOutcome(res.status, result=res, url=url)


# ---------------------------------------------------------------------- 調査結果の保存形式（同じページの再利用用）

def result_to_json(r: ProductPageResult) -> str:
    """抽出結果（自社・Amazon等の除外前の生の結果）をJSONにする。`collector_run.error_message`に保存し、30日の間は
    同じページへ再アクセスせずに、別のroute（SELLER→BRANDなど）の登録に再利用する。"""
    import json

    return json.dumps({
        "v": 1, "status": r.status, "seller_scope": r.seller_scope, "notes": r.notes,
        "sellers": [{"id": s.seller_id, "name": s.name, "region": s.region, "role": s.role} for s in r.sellers],
        "shippers": [{"id": s.seller_id, "name": s.name, "region": s.region, "role": s.role} for s in r.shippers],
        "brands": [{"type": b.source_type, "key": b.key, "basis": b.basis} for b in r.brands],
    }, ensure_ascii=False)


def result_from_json(text_: str | None) -> ProductPageResult | None:
    import json

    if not text_ or not text_.startswith("{"):
        return None
    try:
        d = json.loads(text_)
        if d.get("v") != 1:
            return None
        return ProductPageResult(
            d["status"], [SellerRef(s["id"], s.get("name"), s["region"], s.get("role", "UNSPECIFIED")) for s in d["sellers"]],
            [BrandRef(b["type"], b["key"], b["basis"]) for b in d["brands"]], d["seller_scope"], list(d.get("notes", [])),
            [SellerRef(s["id"], s.get("name"), s["region"], s.get("role", "SHIPS_FROM")) for s in d.get("shippers", [])])
    except (ValueError, KeyError, TypeError):
        return None
