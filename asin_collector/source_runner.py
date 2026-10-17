"""出典単位の取得を、acquisition の実行制御（claim・lease・heartbeat・結果の区別）に接続する。

既存の `collect-brand-page`（旧経路: source_id なしの collector_run）とは別の入口で、**既知の出典を明示して**
取得する。セラーの自動発見や反対側市場への展開はここでは行わない。

流れ: claim（取得間隔・除外・保留・実行中・旧証拠を再判定。ロック下）→ ページ取得 → 1ページごとに
`append_asins`（run行ロック＋トークン＋DB時刻のlease確認のうえ保存）→ `finish`（結果を区別して確定）。
heartbeat は別スレッドで続け、CAPTCHAの人の待機中も切らさない。leaseを失ったら、以後は書き込みも
次のページ取得もしない（`LeaseLost` を報告するだけ。終了確定も試みない）。

結果の区別（`collector_run.outcome`）:
- SUCCESS_NONEMPTY   最後のページまで辿り、ASINがあった（「次へ」が無い）
- SUCCESS_EMPTY      1ページ目が「検索結果0件」ウィジェット付きの0件で、**その種別で0件判定が検証済み**の場合のみ
- SUCCESS_TRUNCATED  ページ数/ASIN数の上限で打ち切った（取得間隔に数えるかは設定 `truncated_counts_as_success`）
- PARTIAL            ASINを保存した後に、失敗・想定外のページ・同一URL再訪で止まった（成功に数えない）
- FAILED             1ページ目から取得できなかった（読み込み失敗・5xx・CAPTCHA未解決）
- UNVERIFIED_EMPTY   0件に見えるが0件と確認できない（ウィジェット無し、または未検証の種別）。**成功にしない**
- INTERRUPTED        実行者のCtrl+C（失敗に数えない）。leaseの失効は別系統（回収）

**実ページで未検証のもの**: 0件ウィジェットでの0件判定が検証済みなのはSELLER（ストアフロント検索）だけ
（docs/DEVIATIONS.md #53）。KEYWORD/BRAND_NAME/BRAND_STOREの0件は、ウィジェットがあっても UNVERIFIED_EMPTY
にする。BRAND_NAME/KEYWORDのURL形式、配送先表示のチェックも実ページでは未検証。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import quote, quote_plus, urlparse

from sqlalchemy import text

from acquisition.lease import AcquisitionRunService, Claim, LeaseLost
from asin_collector.asin_extractor import extract_search_card_asins
from asin_collector.browser_session import html_is_captcha, html_is_server_error
from asin_collector.check_recommendations import NO_RESULTS_MARKER, storefront_search_url
from asin_collector.pagination import find_next_url
from db.session import SessionFactory

DOMAIN = {"US": "www.amazon.com", "JP": "www.amazon.co.jp"}
# 「検索結果0件」ウィジェットでの0件判定が実ページで検証済みの種別（docs/DEVIATIONS.md #53）。
VERIFIED_EMPTY_TYPES = frozenset({"SELLER"})
_ABSOLUTE_PAGE_LIMIT = 200


class PageLoadError(Exception):
    """ページを読み込めなかった（ネットワーク・タイムアウト等）。"""


class CaptchaNotSolved(Exception):
    """CAPTCHAが待機時間内に解かれなかった（回避はしない。人の手動対応が必要）。"""


@dataclass
class FetchedPage:
    html: str
    url: str
    status: int | None = None


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...

    def wait_for_captcha_solved(self) -> None:
        """人がCAPTCHAを解くまで待つ（対話式）。"""


@dataclass
class RunLimits:
    max_pages: int = 5  # 初回の試行は小さく。None相当にはできない（絶対上限は200）
    max_asins: int | None = None
    server_error_retries: int = 2
    server_error_wait_seconds: float = 5.0
    max_captcha_rounds: int = 3


@dataclass
class RunReport:
    run_id: int
    source_id: int
    outcome: str  # SUCCESS_* / PARTIAL / FAILED / UNVERIFIED_EMPTY / INTERRUPTED / LEASE_LOST
    pages: int = 0
    asins: int = 0
    error_code: str | None = None
    detail: str | None = None
    auto_held: bool = False


@dataclass
class SourceRow:
    source_id: int
    source_type: str
    key_primary: str
    display_primary: str
    display_qualifier: str


def load_source(app: SessionFactory, marketplace: str, source_id: int) -> SourceRow:
    with app.get_session(marketplace) as conn, conn.begin():
        r = conn.execute(
            text("SELECT source_id, source_type, key_primary, display_primary, display_qualifier "
                 "FROM acquisition_source WHERE source_id = :id"),
            {"id": source_id},
        ).one_or_none()
    if r is None:
        raise ValueError(f"source_id={source_id} は市場{marketplace}に存在しません")
    return SourceRow(r.source_id, r.source_type, r.key_primary, r.display_primary, r.display_qualifier)


def source_url(marketplace: str, src: SourceRow) -> str:
    """出典の取得開始URL。取得先市場のドメインに限る（市場の取り違えを防ぐ）。
    KEYWORD/BRAND_NAMEのURL形式は実ページ未検証（BRAND_NAMEは旧実行のURLに見られる rh=p_89 を使う）。"""
    if marketplace not in DOMAIN:
        raise ValueError(f"unknown marketplace: {marketplace!r}")
    host = DOMAIN[marketplace]
    if src.source_type == "SELLER":
        return storefront_search_url(marketplace, src.key_primary)
    if src.source_type == "KEYWORD":
        return f"https://{host}/s?k={quote_plus(src.display_primary)}"
    if src.source_type == "BRAND_NAME":
        return f"https://{host}/s?rh=p_89%3A{quote(src.display_primary, safe='')}"
    if src.source_type == "BRAND_STORE":
        parsed = urlparse(src.display_primary)
        if (parsed.hostname or "").lower() not in (host, host.removeprefix("www.")):
            raise ValueError(f"ストアURLのドメインが取得先市場{marketplace}と一致しません: {src.display_primary}")
        return src.display_primary
    raise ValueError(f"unsupported source_type: {src.source_type!r}")


def html_has_no_results_widget(html: str) -> bool:
    return NO_RESULTS_MARKER in html


# ---------------------------------------------------------------------- heartbeat

class Heartbeat:
    """別スレッドでleaseを延長し続ける。LeaseLostになったら lost を立てて止まる（復活させない）。"""

    def __init__(self, service: AcquisitionRunService, claim: Claim, interval: float | None = None) -> None:
        self._service, self._claim = service, claim
        self._interval = interval if interval is not None else max(1.0, claim.lease_seconds / 3)
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._service.heartbeat(self._claim)
            except LeaseLost:
                self.lost.set()
                return
            except Exception:  # noqa: BLE001 - 一時的なDBエラーは次の周期で再試行（失効すれば次の操作がLeaseLostになる）
                continue


# ---------------------------------------------------------------------- 実行

class _Stop(Exception):
    def __init__(self, outcome: str, code: str | None = None, detail: str | None = None) -> None:
        self.outcome, self.code, self.detail = outcome, code, detail
        super().__init__(outcome)


def run_claimed_source(
    service: AcquisitionRunService,
    app: SessionFactory,
    fetcher: PageFetcher,
    claim: Claim,
    *,
    limits: RunLimits | None = None,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat_interval: float | None = None,
) -> RunReport:
    """claim済みの出典を取得して結果を確定する。leaseを失ったら LEASE_LOST を返す（書き込み・終了確定はしない）。
    実行者のCtrl+Cは INTERRUPTED(USER) として確定してから再送出する。"""
    limits = limits or RunLimits()
    marketplace = claim.marketplace
    src = load_source(app, marketplace, claim.source_id)
    report = RunReport(claim.run_id, claim.source_id, "FAILED")
    try:
        with Heartbeat(service, claim, heartbeat_interval) as hb:
            try:
                outcome, trunc = _crawl(service, fetcher, claim, src, source_url(marketplace, src), limits, sleep, hb, report)
            except _Stop as stop:
                outcome, trunc = stop.outcome, None
                report.error_code, report.detail = stop.code, stop.detail
            if hb.lost.is_set():
                raise LeaseLost("heartbeatでleaseの喪失を検知しました")
            held = service.finish(claim, outcome, truncation_reason=trunc, error_code=report.error_code,
                                  error_message=report.detail, pages_fetched=report.pages)
            report.outcome, report.auto_held = outcome, held is not None
    except LeaseLost as exc:
        report.outcome, report.detail = "LEASE_LOST", str(exc)
    except KeyboardInterrupt:
        service.interrupt(claim, "USER")
        report.outcome = "INTERRUPTED"
        raise
    except Exception as exc:  # noqa: BLE001 - 想定外の失敗は結果として確定し、呼び出し側のバッチは継続できるようにする
        outcome = "PARTIAL" if report.asins > 0 else "FAILED"
        try:
            held = service.finish(claim, outcome, error_code="EXCEPTION", error_message=f"{type(exc).__name__}: {exc}"[:500],
                                  pages_fetched=report.pages)
            report.outcome, report.auto_held = outcome, held is not None
        except LeaseLost as lost:
            report.outcome, report.detail = "LEASE_LOST", str(lost)
        else:
            report.error_code, report.detail = "EXCEPTION", f"{type(exc).__name__}: {exc}"
    return report


def _get(service, fetcher, claim, url, limits, sleep, hb, pages_saved: int) -> FetchedPage:
    """1ページ取得。5xxは自動リトライ、CAPTCHAは人の待機（heartbeat継続）。失敗は _Stop で上位へ。"""
    fail_outcome = "PARTIAL" if pages_saved > 0 else "FAILED"
    captcha_rounds = 0
    attempt = 0
    while True:
        if hb.lost.is_set():
            raise LeaseLost("取得中にleaseを失いました")
        try:
            page = fetcher.fetch(url)
        except PageLoadError as exc:
            raise _Stop(fail_outcome, "PAGE_LOAD_FAILED", str(exc)[:300]) from exc
        if html_is_captcha(page.html):
            captcha_rounds += 1
            if captcha_rounds > limits.max_captcha_rounds:
                raise _Stop(fail_outcome, "CAPTCHA_UNSOLVED", f"CAPTCHAが{limits.max_captcha_rounds}回解消しませんでした")
            service.enter_captcha_wait(claim)  # leaseを長めに延ばしてDB上でも見える化
            try:
                fetcher.wait_for_captcha_solved()
            except CaptchaNotSolved as exc:
                raise _Stop(fail_outcome, "CAPTCHA_UNSOLVED", str(exc)[:300]) from exc
            service.leave_captcha_wait(claim)
            continue
        if (page.status is not None and page.status >= 500) or html_is_server_error(page.html):
            attempt += 1
            if attempt <= limits.server_error_retries:
                sleep(limits.server_error_wait_seconds)
                continue
            raise _Stop(fail_outcome, "SERVER_ERROR", f"5xx/エラーページが{attempt}回続きました（status={page.status}）")
        return page


def _crawl(service, fetcher, claim, src, start_url, limits, sleep, hb, report: RunReport) -> tuple[str, str | None]:
    visited: set[str] = set()
    seen: set[str] = set()
    target = start_url
    max_pages = min(limits.max_pages, _ABSOLUTE_PAGE_LIMIT)
    while True:
        page = _get(service, fetcher, claim, target, limits, sleep, hb, report.pages)
        if page.url in visited:
            raise _Stop("PARTIAL", "REVISITED_URL", f"同じURLに戻りました: {page.url}")
        visited.add(page.url)
        cards = extract_search_card_asins(page.html)
        if not cards:
            if report.pages > 0:
                raise _Stop("PARTIAL", "EMPTY_PAGE_AFTER_RESULTS", "結果の途中で0件のページになりました（想定外）")
            if html_has_no_results_widget(page.html) and src.source_type in VERIFIED_EMPTY_TYPES:
                return "SUCCESS_EMPTY", None
            code = "EMPTY_NOT_VERIFIED_FOR_TYPE" if html_has_no_results_widget(page.html) else "NO_CARDS_NO_WIDGET"
            raise _Stop("UNVERIFIED_EMPTY", code, "0件に見えるが、0件と確認できません（成功に数えません）")
        new = [a for a in cards if a not in seen]
        seen.update(new)
        service.append_asins(claim, new, pages=1)  # lease・トークンを確認して保存。失効なら LeaseLost
        report.pages += 1
        report.asins += len(new)
        nxt = find_next_url(page.html, page.url)
        if not nxt:
            return "SUCCESS_NONEMPTY", None
        if report.pages >= max_pages:
            return "SUCCESS_TRUNCATED", "PAGE_LIMIT"
        if limits.max_asins is not None and report.asins >= limits.max_asins:
            return "SUCCESS_TRUNCATED", "ASIN_LIMIT"
        target = nxt


# ---------------------------------------------------------------------- 実ブラウザ

def delivery_region_warning(html: str, marketplace: str) -> str | None:
    """配送先表示が想定の郵便番号と違えば警告文を返す（警告のみ。自動操作はしない。CLAUDE.md 7節）。
    **実ページのDOMは未検証**: 表示ブロック(#glow-ingress-block)が見つからない場合も警告にする。"""
    from selectolax.parser import HTMLParser

    expected = {"US": "97005", "JP": "289-1223"}[marketplace]
    node = HTMLParser(html).css_first("#glow-ingress-block")
    shown = node.text(separator=" ") if node is not None else ""
    digits = "".join(ch for ch in shown if ch.isdigit())
    if "".join(ch for ch in expected if ch.isdigit()) in digits:
        return None
    return (f"配送先の表示が想定（{expected}）と確認できません。市場{marketplace}のブラウザプロファイルの"
            "お届け先地域を手動で設定してください（表示ブロックの検出は実ページ未検証）")


class PlaywrightFetcher:
    """実ブラウザ（市場別の永続プロファイル）で取得する。対話式のため headed。"""

    def __init__(self, page, captcha_timeout: float = 300.0) -> None:
        self._page = page
        self._captcha_timeout = captcha_timeout

    def fetch(self, url: str) -> FetchedPage:
        try:
            resp = self._page.goto(url)
        except Exception as exc:  # noqa: BLE001
            raise PageLoadError(f"{type(exc).__name__}: {exc}") from exc
        return FetchedPage(self._page.content(), self._page.url, resp.status if resp is not None else None)

    def wait_for_captcha_solved(self) -> None:
        """CAPTCHAは回避しない。対話端末ならEnter待ち、端末が無い（標準入力が使えない）実行では、ブラウザ画面で
        人が解くのを一定時間ポーリングして待つ（解けなければ CaptchaNotSolved）。"""
        import sys

        from asin_collector.browser_session import is_captcha_page, wait_for_human_to_solve_captcha

        print("【手動対応が必要】CAPTCHAが表示されています。ブラウザ画面で解いてください（自動では回避しません）。", flush=True)
        if sys.stdin is not None and sys.stdin.isatty():
            wait_for_human_to_solve_captcha(self._page)
            return
        deadline = time.monotonic() + self._captcha_timeout
        while time.monotonic() < deadline:
            time.sleep(5)
            if not is_captcha_page(self._page):
                return
        raise CaptchaNotSolved(f"CAPTCHAが{int(self._captcha_timeout)}秒以内に解かれませんでした")

    def delivery_warning(self, marketplace: str) -> str | None:
        try:
            self._page.goto(f"https://{DOMAIN[marketplace]}/")
            return delivery_region_warning(self._page.content(), marketplace)
        except Exception:  # noqa: BLE001
            return "配送先表示を確認できませんでした（警告のみ）"


# ---------------------------------------------------------------------- 対象の選択と連続実行

@dataclass
class BatchItem:
    source_id: int
    label: str
    result: str  # 'RAN' / 'SKIPPED'
    report: RunReport | None = None
    reason: str | None = None


def select_targets(plan, section: str, max_sources: int, *, include_zero_units: bool = False,
                   include_no_origin: bool = False) -> list:
    """planの順位をそのまま引き継いで、実行対象を絞る（無制限に広げない）。
    注文点数0点の起点しか持たない出典・起点のない出典は、既定では対象にしない（経路としては残る）。"""
    if max_sources < 1 or max_sources > MAX_SOURCES_HARD_CAP:
        raise ValueError(f"max_sources は1〜{MAX_SOURCES_HARD_CAP}で指定してください")
    entries = list(plan.sections.get(section, []))
    if include_no_origin and section != "NO_ORIGIN":
        entries += list(plan.sections.get("NO_ORIGIN", []))
    out, seen = [], set()
    for e in entries:
        if e.facts.source_id in seen:
            continue
        if e.rep is None:
            if not include_no_origin:
                continue
        elif e.rep.units <= 0 and not include_zero_units:
            continue
        seen.add(e.facts.source_id)
        out.append(e)
        if len(out) >= max_sources:
            break
    return out


MAX_SOURCES_HARD_CAP = 20


def run_batch(
    service: AcquisitionRunService,
    app: SessionFactory,
    fetcher: PageFetcher,
    marketplace: str,
    source_ids: list[int],
    *,
    limits: RunLimits | None = None,
    stop_after_failures: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat_interval: float | None = None,
) -> list[BatchItem]:
    """出典を順に claim して取得する。claim できない出典（間隔内・保留・実行中等）は理由を残して飛ばす。
    連続して成功しない実行が stop_after_failures 件続いたら、残りを実行せず止める（ブラウザ・Amazon側の異常の疑い）。
    leaseを失った実行があれば以後の実行もやめる。Ctrl+Cは中断として確定したうえで上位へ送出する。"""
    from acquisition.lease import NotEligible

    items: list[BatchItem] = []
    streak = 0
    for sid in source_ids:
        try:
            claim = service.claim(marketplace, sid)
        except NotEligible as exc:
            items.append(BatchItem(sid, str(sid), "SKIPPED", reason=str(exc)))
            continue
        report = run_claimed_source(service, app, fetcher, claim, limits=limits, sleep=sleep,
                                    heartbeat_interval=heartbeat_interval)
        items.append(BatchItem(sid, str(sid), "RAN", report))
        if report.outcome == "LEASE_LOST":
            break
        streak = 0 if report.outcome.startswith("SUCCESS") else streak + 1
        if streak >= stop_after_failures:
            break
    return items
