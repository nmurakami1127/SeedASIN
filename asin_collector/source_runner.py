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
- SUCCESS_TRUNCATED  ページ数/ASIN数の上限で打ち切った（取得間隔に数えるかは設定 `truncated_counts_as_success`）。
                     設定 `close_on_repeated_page`=true のときは、検索結果の深さの限界（DEPTH_LIMIT。同じページの繰り返しを
                     1回の取り直しで確認）でも、再開位置なし（CLOSED_NO_CURSOR）で閉じる（docs/DEVIATIONS.md #80）
- PARTIAL            ASINを保存した後に、失敗・想定外のページ・同一URL再訪で止まった（成功に数えない）
- FAILED             1ページ目から取得できなかった（読み込み失敗・5xx・CAPTCHA未解決）
- UNVERIFIED_EMPTY   0件に見えるが0件と確認できない（ウィジェット無し、または未検証の種別）。**成功にしない**
- INTERRUPTED        実行者のCtrl+C（失敗に数えない）。leaseの失効は別系統（回収）
- DAILY_LIMIT        日次のページ枠が尽きて止めた（INTERRUPTED(DAILY_LIMIT)として確定。失敗に数えず、保存済みの位置から再開する）

価格帯分割（`RunLimits.price_band`、既定は無効。docs/DEVIATIONS.md #83）: 有効なとき、JP/SELLER の一巡を価格帯ごとに走査する。
一巡＝セラー1件の全帯の走査で、帯は一巡の内側。初期帯を lo 昇順に走査し、DEPTH_LIMIT（同じページの繰り返しを1回の取り直しで確認）を
確認した帯だけをその場で二分割する。最小幅/最大深さで割れない帯は TRUNCATED_MIN として閉じて次へ進む。全帯が終われば
SUCCESS_NONEMPTY（TRUNCATED_MIN があれば completeness='TRUNCATED'・truncation_reason='BAND_MIN_WIDTH'）で一巡を完了にする。
帯の状態は acquisition_band（DB）が真実で、どこで中断しても CONTINUE で続けられる。帯なしの一巡は従来どおり（`_crawl_banded`とは別経路）。

続き取得（Claim.plan）: CONTINUE=保存済みの次ページから / FINALIZE=末尾到達を保存済みで終了確定前に止まった一巡を、取得せずに完了として確定 /
RETRY_FIRST=保存済みページが無い一巡を1ページ目から（初回と同じ判定。確認済みの0件はSUCCESS_EMPTY）。

**実ページで未検証のもの**: 0件ウィジェットでの0件判定が検証済みなのはSELLER（ストアフロント検索）だけ
（docs/DEVIATIONS.md #53）。KEYWORD/BRAND_NAME/BRAND_STOREの0件は、ウィジェットがあっても UNVERIFIED_EMPTY
にする。BRAND_NAME/KEYWORDのURL形式、配送先表示のチェックも実ページでは未検証。
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import quote, quote_plus, urlparse

from sqlalchemy import text

from acquisition.lease import AcquisitionRunService, Claim, LeaseLost, PageCursor
from acquisition.partition import Band as PBand
from acquisition.partition import CoverError, PartitionConfig, PriceBandPartition, check_cover, get_strategy
from asin_collector.asin_extractor import extract_search_card_asins
from asin_collector.browser_session import html_is_captcha, html_is_server_error
from asin_collector.check_recommendations import NO_RESULTS_MARKER, storefront_search_url
from asin_collector.pagination import find_next_url
from db.session import SessionFactory

DOMAIN = {"US": "www.amazon.com", "JP": "www.amazon.co.jp"}
# 「検索結果0件」ウィジェットでの0件判定が実ページで検証済みの種別（docs/DEVIATIONS.md #53）。
VERIFIED_EMPTY_TYPES = frozenset({"SELLER"})
_ABSOLUTE_PAGE_LIMIT = 200
OVERLAP_MIN_RATIO = 0.2  # 重なり確認で、前回の最後のページのASINがこの割合未満しか再び見えないなら、ページ送りの位置を信頼しない（BROKEN）


class PageLoadError(Exception):
    """ページを読み込めなかった（ネットワーク・タイムアウト等）。"""


class CaptchaNotSolved(Exception):
    """CAPTCHAが待機時間内に解かれなかった（回避はしない。人の手動対応が必要）。"""


class AccessBudgetExhausted(PageLoadError):
    """日次のページ枠を使い切ったため、ページを開かなかった（取得先の失敗ではない）。
    PageLoadErrorの派生にしているのは、商品ページの調査など、これを区別しない既存の呼び出し側が従来どおり動くため。"""


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
    # 既定false=従来どおり（REPEATED_PAGEで一巡をBROKENにする）。true=保存済みのページがある状態で直前と同じページが返ったら、
    # 同じURLを1回だけ取り直し、同じなら「検索結果の深さの限界」として SUCCESS_TRUNCATED / DEPTH_LIMIT / 再開位置なしで閉じる。
    # しきい値（ページ数など）は設けない。価格帯分割はしない（別案件）。
    close_on_repeated_page: bool = False
    # None=帯なし（従来どおり）。設定すると、**JP/SELLER の一巡を価格帯ごとに走査する**（登録の無い市場・種別では無視して従来どおり）。
    # close_on_repeated_page=true が前提（帯の分割は DEPTH_LIMIT の確認を使う）。docs/DEVIATIONS.md #83
    price_band: PartitionConfig | None = None

    def __post_init__(self) -> None:
        if self.price_band is not None and not self.close_on_repeated_page:
            raise ValueError("price_band は close_on_repeated_page=true が前提です（帯の分割は繰り返しページの確認を使います）")


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
    overlap_pages: int = 0       # 続きの前に取り直したページ数（日次のページ使用量に含める）
    repeat_check_pages: int = 0  # 繰り返しの確認のために取り直したページ数（close_on_repeated_page。日次のページ使用量に含める。pages・overlap_pagesとは別）
    truncation_reason: str | None = None  # SUCCESS_TRUNCATEDの理由（PAGE_LIMIT / ASIN_LIMIT / DEPTH_LIMIT）
    drift: bool = False          # 重なり確認で、検索結果の変化を検知した（欠落の可能性）
    plan: str = "FRESH"          # FRESH / CONTINUE / FINALIZE / RETRY_FIRST / RESTART_*
    cycle_id: int | None = None
    completeness: str | None = None  # 帯あり一巡で、outcomeの既定と違う完全性を記録するとき（'TRUNCATED'）。None=outcomeから決まる既定
    banded: bool = False             # 帯あり（価格帯）で走査した
    band_splits: int = 0             # この実行で二分割した帯の数
    band_min_width: int = 0          # この実行で最小幅/最大深さのため打ち切った帯（TRUNCATED_MIN）の数
    band_empty: int = 0              # この実行で空（確認済みの0件）として閉じた帯の数


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
    def __init__(self, outcome: str, code: str | None = None, detail: str | None = None, *,
                 truncation: str | None = None, drop_cursor: bool = False) -> None:
        self.outcome, self.code, self.detail = outcome, code, detail
        self.truncation, self.drop_cursor = truncation, drop_cursor  # 成功としての打切り(DEPTH_LIMIT)で使う
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
    report = RunReport(claim.run_id, claim.source_id, "FAILED", plan=claim.plan, cycle_id=claim.cycle_id)
    try:
        with Heartbeat(service, claim, heartbeat_interval) as hb:
            drop_cursor = False
            try:
                outcome, trunc = _crawl(service, fetcher, claim, src, source_url(marketplace, src), limits, sleep, hb, report)
            except _Stop as stop:
                outcome, trunc, drop_cursor = stop.outcome, stop.truncation, stop.drop_cursor
                report.error_code = stop.code
                report.detail = stop.detail
            report.truncation_reason = trunc
            if hb.lost.is_set():
                raise LeaseLost("heartbeatでleaseの喪失を検知しました")
            if outcome == "DAILY_LIMIT":  # 失敗ではない: 中断として確定し、一巡はOPENのまま（位置は保存済みの最後のページの次）
                if not service.interrupt(claim, "DAILY_LIMIT", report.detail):
                    raise LeaseLost("日次枠での中断の確定時に、leaseを失っていました")
                report.outcome = "DAILY_LIMIT"
                return report
            extra = {"completeness": report.completeness} if report.completeness else {}  # 帯ありの一巡だけ（従来の呼び出しは変えない）
            held = service.finish(claim, outcome, truncation_reason=trunc, error_code=report.error_code,
                                  error_message=None if drop_cursor else report.detail, pages_fetched=report.pages,
                                  drop_cursor=drop_cursor, **extra)
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
        except AccessBudgetExhausted as exc:
            raise _Stop("DAILY_LIMIT", "DAILY_LIMIT", str(exc)[:300]) from exc
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


def _signature(cards: list[str]) -> str:
    return hashlib.sha256("\n".join(cards).encode()).hexdigest()


def _empty_page(src, page: FetchedPage, report: RunReport, continuing: bool) -> tuple[str, str | None]:
    """カードが1件も無いページの扱い（従来どおり）。0件の確認は1ページ目（保存済みページが無い）だけ。"""
    if report.pages > 0:
        raise _Stop("PARTIAL", "EMPTY_PAGE_AFTER_RESULTS", "結果の途中で0件のページになりました（想定外）")
    if continuing:
        raise _Stop("PARTIAL", "CURSOR_INVALID", "保存した次ページのURLを開いても結果がありません（次ページ位置が無効）。正常な0件・完了とは扱いません")
    if html_has_no_results_widget(page.html) and src.source_type in VERIFIED_EMPTY_TYPES:
        return "SUCCESS_EMPTY", None
    code = "EMPTY_NOT_VERIFIED_FOR_TYPE" if html_has_no_results_widget(page.html) else "NO_CARDS_NO_WIDGET"
    raise _Stop("UNVERIFIED_EMPTY", code, "0件に見えるが、0件と確認できません（成功に数えません）")


def _crawl(service, fetcher, claim, src, start_url, limits, sleep, hb, report: RunReport) -> tuple[str, str | None]:
    """ページを辿り、1ページごとに ASINの保存と再開位置の更新を同じトランザクションで行う。
    続き(CONTINUE)では、前回の最後に保存したページを `overlap_pages` だけ取り直して（ASINは入れない）ページ送りが有効か確認し、
    保存済みの次ページから続ける。取得済みのASINは重ねて保存しない。ループする次ページ・繰り返すページ・無効な次ページは
    BROKEN（一巡を完了にも正常な0件にもしない）。検索結果は時間で変わるため、これで欠落が無いとは保証できない（driftとして記録する）。"""
    band_cfg = _band_config(service, claim, src, limits)
    if claim.plan == "FINALIZE":
        # 前回のrunが末尾のページを正常に解析・保存し（end_reached）、終了確定の前に止まった。取得し直さずに、この一巡を完了として確定する
        # （30日間隔の起点は、この確定の時刻）。前回のrunの行は書き換えない（終了後の行は保護トリガーで不変）。
        if band_cfg is not None:
            return _finalize_banded(service, claim, report, band_cfg)
        return "SUCCESS_NONEMPTY", None
    if band_cfg is not None:
        return _crawl_banded(service, fetcher, claim, src, start_url, limits, sleep, hb, report, band_cfg)
    # 途中の継続取得は、保存済みページがある一巡だけ（RETRY_FIRSTは保存済みページが無いので、1ページ目から初回と同じ判定をする）
    continuing = claim.plan == "CONTINUE" and bool(claim.resume_url)
    seen: set[str] = service.cycle_asins(claim) if continuing else set()
    visited: set[str] = set()
    target = claim.resume_url if continuing else start_url
    page_no = (claim.resume_page_no or 1) if continuing else 1
    if not continuing:
        service.set_start(claim, start_url, 1)
    prev_sig = claim.last_page_signature if continuing else None
    if continuing and claim.overlap_pages > 0 and claim.last_page_url:
        ov = _get(service, fetcher, claim, claim.last_page_url, limits, sleep, hb, report.pages)
        cards0 = extract_search_card_asins(ov.html)
        stored = set(claim.last_page_asins or ())
        ratio = (len(set(cards0) & stored) / len(stored)) if stored else (1.0 if cards0 else 0.0)
        drift = ratio < 1.0 or (claim.last_page_signature is not None and _signature(cards0) != claim.last_page_signature)
        report.overlap_pages += 1
        service.record_overlap(claim, ratio=ratio, drift=drift)
        report.drift = report.drift or drift
        if ratio < OVERLAP_MIN_RATIO:
            raise _Stop("PARTIAL", "OVERLAP_MISMATCH", f"前回の最後のページを取り直したところ、ASINの重なりが{ratio:.0%}しかありません。ページ送りの位置を信頼できません")
        visited.add(ov.url)
    elif continuing and claim.last_page_url:
        visited.add(claim.last_page_url)
    max_pages = min(limits.max_pages, _ABSOLUTE_PAGE_LIMIT)
    zero_new = 0
    while True:
        page = _get(service, fetcher, claim, target, limits, sleep, hb, report.pages)
        if page.url in visited:
            raise _Stop("PARTIAL", "REVISITED_URL", f"同じURLに戻りました: {page.url}")
        visited.add(page.url)
        cards = extract_search_card_asins(page.html)
        if not cards:
            return _empty_page(src, page, report, continuing)
        sig = _signature(cards)
        if sig == prev_sig:
            if not limits.close_on_repeated_page:
                raise _Stop("PARTIAL", "REPEATED_PAGE", "直前のページと同じ内容が繰り返されました（ページ送りが進んでいません）")
            # 保存済みのページ（この実行、または続きの前回まで）の直後に、同じ内容が返った。一時的な不具合かもしれないので、
            # 同じURLを**1回だけ**取り直して確認する（_getを通す: CAPTCHA・5xx・日次枠・heartbeatは他のページと同じ。日次枠にも1ページとして数える）。
            page = _get(service, fetcher, claim, target, limits, sleep, hb, report.pages)
            report.repeat_check_pages += 1
            cards = extract_search_card_asins(page.html)
            if not cards:  # 取り直したら0件のページになった: 限界ではなく想定外（従来の0件ページと同じ扱い）
                return _empty_page(src, page, report, continuing)
            sig = _signature(cards)
            if sig == prev_sig:
                raise _Stop("SUCCESS_TRUNCATED", None,
                            f"同じ内容が取り直しても繰り返されたため、検索結果の深さの限界として閉じます（保存{report.pages}ページ・取り直し{report.repeat_check_pages}ページ）",
                            truncation="DEPTH_LIMIT", drop_cursor=True)
            # 取り直したら内容が変わっていた（一時的な不具合）: 通常のページとして処理を続ける
        prev_sig = sig
        new = [a for a in cards if a not in seen]
        zero_new = zero_new + 1 if not new else 0
        if zero_new >= 2:
            raise _Stop("PARTIAL", "NO_NEW_ASINS_LOOP", "新しいASINが2ページ続けて0件でした（ループの疑い）")
        seen.update(new)
        nxt = find_next_url(page.html, page.url)
        cursor = PageCursor(nxt, page_no + 1 if nxt else None, page.url, sig, tuple(cards), end_reached=not nxt)
        service.append_asins(claim, new, pages=1, cursor=cursor)  # lease・トークンを確認し、ASIN保存と再開位置を同じトランザクションで保存
        report.pages += 1
        report.asins += len(new)
        page_no += 1
        if not nxt:
            return "SUCCESS_NONEMPTY", None
        if report.pages >= max_pages:
            return "SUCCESS_TRUNCATED", "PAGE_LIMIT"
        if limits.max_asins is not None and report.asins >= limits.max_asins:
            return "SUCCESS_TRUNCATED", "ASIN_LIMIT"
        target = nxt


# ---------------------------------------------------------------------- 価格帯あり（docs/DEVIATIONS.md #83）

def _band_config(service, claim, src, limits: RunLimits) -> PartitionConfig | None:
    """この一巡を帯ありで走査するなら、その設定を返す（帯なしは None＝従来どおり）。
    一巡の方式は**開始時に決まり、途中で変えない**: 続き(CONTINUE)・FINALIZE・RETRY_FIRSTでは、この一巡に帯があれば設定が無効になっていても
    帯ありで続ける（帯の状態はDBが真実。設定を外しても途中の一巡を壊さない）。帯の無い一巡は、設定が有効になっても従来どおり最後まで進める
    （保存済みページのある一巡を帯ありで最初からやり直さない）。登録の無い市場・種別（US、JP以外の種別）は常に帯なし。"""
    strat = get_strategy(claim.marketplace, src.source_type)
    if strat is None or not strat.applies(claim.marketplace, src.source_type):
        return None
    if claim.plan in ("CONTINUE", "FINALIZE", "RETRY_FIRST") and claim.cycle_id is not None:
        if service.load_bands(claim):
            return limits.price_band or PartitionConfig()
        return limits.price_band if claim.plan == "RETRY_FIRST" else None
    return limits.price_band


class _Bands:
    """一巡の帯をメモリに持つ（DBが真実。advance_band などの戻り値で更新する）。"""

    def __init__(self, service, claim, strat: PriceBandPartition, base_url: str, cfg: PartitionConfig) -> None:
        self.service, self.claim, self.strat, self.base, self.cfg = service, claim, strat, base_url, cfg
        self.list: list = []

    def url(self, b) -> str:
        return self.strat.url(self.base, PBand(b.lo_units, b.hi_units))

    def active(self):
        return next((b for b in self.list if b.state == "ACTIVE"), None)

    def pending(self) -> list:
        return sorted((b for b in self.list if b.state == "PENDING"), key=lambda b: b.lo_units)

    def next_active(self):
        """ACTIVEな帯（分割直後の左の子など）を返す。無ければ待機中の先頭をACTIVEにして返す。全帯が終わっていれば None。"""
        act = self.active()
        if act is not None:
            return act
        pend = self.pending()
        if not pend:
            return None
        self.list = self.service.advance_band(self.claim, pend[0].band_id, "ACTIVE")
        return self.active()

    def split_of(self, b) -> tuple | None:
        return self.strat.split(PBand(b.lo_units, b.hi_units, b.depth))

    def close_by_depth(self, b, report: RunReport) -> None:
        """検索結果の深さの限界を確認した帯を閉じる: 割れれば二分割（親SPLIT・子2つ・左がACTIVE）、割れなければ TRUNCATED_MIN。"""
        sp = self.split_of(b)
        if sp is None:
            self.list = self.service.advance_band(self.claim, b.band_id, "TRUNCATED_MIN", close_reason="DEPTH_LIMIT_MIN_WIDTH")
            report.band_min_width += 1
        else:
            self.list = self.service.advance_band(self.claim, b.band_id, "SPLIT", close_reason="DEPTH_LIMIT", split_at=sp[0].hi)
            report.band_splits += 1

    def close_at_end(self, b, pages: int, report: RunReport) -> None:
        """帯の末尾（「次へ」が無い）に着いた。設定 treat_end_as_depth_limit_pages 以上のページ数なら深さの限界として扱い、そうでなければ完了。"""
        n = self.cfg.treat_end_as_depth_limit_pages
        if n is not None and pages >= n:
            self.close_by_depth(b, report)
        else:
            self.list = self.service.advance_band(self.claim, b.band_id, "DONE", close_reason="END_OF_RESULTS")


def _band_result(bs: _Bands, report: RunReport) -> tuple[str, str | None]:
    """全帯が終わった一巡の結果。葉の帯が範囲を隙間なく覆っていることを確認する。TRUNCATED_MIN があれば完全性だけ 'TRUNCATED' にする。"""
    bands = bs.service.load_bands(bs.claim)
    leaves = [b for b in bands if b.state != "SPLIT"]
    try:
        check_cover([PBand(b.lo_units, b.hi_units) for b in leaves], lo=min(b.lo_units for b in leaves), hi=None)
    except CoverError as exc:
        raise _Stop("PARTIAL", "BAND_COVER_BROKEN", f"帯が範囲を隙間なく覆っていません: {exc}") from exc
    if any(b.state not in ("DONE", "TRUNCATED_MIN") for b in leaves):
        raise _Stop("PARTIAL", "BAND_NOT_FINISHED", "終了していない帯が残っています")
    outcome = "SUCCESS_NONEMPTY" if sum(b.pages_fetched for b in bands) > 0 else "SUCCESS_EMPTY"
    if any(b.state == "TRUNCATED_MIN" for b in leaves):
        report.completeness = "TRUNCATED"
        return outcome, "BAND_MIN_WIDTH"
    return outcome, None


def _finalize_banded(service, claim, report: RunReport, cfg: PartitionConfig) -> tuple[str, str | None]:
    """末尾到達を保存済みで終了確定の前に止まった帯ありの一巡: 取得せずに、最後の帯を閉じて完了として確定する。"""
    report.banded = True
    bs = _Bands(service, claim, PriceBandPartition(cfg), "", cfg)
    bs.list = service.load_bands(claim)
    act = bs.active()
    if act is not None:  # 最後のページの保存の後、帯を閉じる前に止まった
        bs.close_at_end(act, act.pages_fetched, report)
    if bs.active() is not None or bs.pending():
        raise _Stop("PARTIAL", "BAND_NOT_FINISHED", "末尾到達の保存後に、終了していない帯が残っています")
    return _band_result(bs, report)


def _crawl_banded(service, fetcher, claim, src, base_url, limits, sleep, hb, report: RunReport, cfg: PartitionConfig) -> tuple[str, str | None]:
    """価格帯ありの一巡。帯は lo 昇順に1つずつ走査する（一巡の中でACTIVEな帯は高々1つ）。

    - 初期帯から始め、DEPTH_LIMIT（保存済みの直後に同じ内容が返り、同じURLの取り直しでも同じ）を確認した帯だけをその場で二分割する。
    - 帯の末尾で DONE にして次の待機帯へ。カーソルの next_url は次の帯の1ページ目（next_page_no=1）、end_reached は最後の帯の末尾のときだけ。
    - 帯の切替（分割・末尾・空の帯）で prev_sig をリセットし、重なり確認(OVERLAP)は同じ帯の続きのときだけ行う。
    - NO_NEW_ASINS_LOOP は使わない（子帯の先頭は親と大きく重複するため）。ループは REPEATED_PAGE の確認と REVISITED_URL で検知する。
    - 再開の位置は acquisition_band（DB）から決める。カーソルは同じ帯の中の続きのときだけ使い、分割直後・帯の切替直後は帯の1ページ目から。
    - ページ数/ASIN数の上限は従来どおり SUCCESS_TRUNCATED で OPEN のまま閉じる（続きは帯の途中から）。"""
    report.banded = True
    strat = PriceBandPartition(cfg)
    bs = _Bands(service, claim, strat, base_url, cfg)
    continuing = claim.plan == "CONTINUE" and bool(claim.resume_url)
    seen: set[str] = service.cycle_asins(claim) if continuing else set()
    visited: set[str] = set()
    bs.list = service.init_bands(claim, [(b.lo, b.hi) for b in strat.initial(cfg)])  # 冪等: 既にこの一巡に帯があれば何も作らない
    resume_band = strat.band_of_url(claim.resume_url) if continuing else None

    # --- 開始位置: DBの帯が真実。ACTIVEな帯（無ければ待機中の先頭を開く）。保存済みページのある帯でカーソルが帯の外にあれば、末尾に着いた後で止まっていた
    in_resume = False
    while True:
        act = bs.active()
        if act is None:
            act = bs.next_active()
            break
        if act.pages_fetched == 0:
            break
        if resume_band is not None and (resume_band.lo, resume_band.hi) == (act.lo_units, act.hi_units):
            in_resume = True
            break
        bs.close_at_end(act, act.pages_fetched, report)  # 帯の末尾のページを保存した後、帯を閉じる前に止まっていた
    if act is None:
        return _band_result(bs, report)

    if in_resume:
        target, page_no, prev_sig = claim.resume_url, (claim.resume_page_no or 1), claim.last_page_signature
    else:
        target, page_no, prev_sig = bs.url(act), 1, None
        if not continuing:
            service.set_start(claim, target, 1)
    cur_pages = act.pages_fetched
    if in_resume and claim.overlap_pages > 0 and claim.last_page_url:
        ov = _get(service, fetcher, claim, claim.last_page_url, limits, sleep, hb, report.pages)
        cards0 = extract_search_card_asins(ov.html)
        stored = set(claim.last_page_asins or ())
        ratio = (len(set(cards0) & stored) / len(stored)) if stored else (1.0 if cards0 else 0.0)
        drift = ratio < 1.0 or (claim.last_page_signature is not None and _signature(cards0) != claim.last_page_signature)
        report.overlap_pages += 1
        service.record_overlap(claim, ratio=ratio, drift=drift)
        report.drift = report.drift or drift
        if ratio < OVERLAP_MIN_RATIO:
            raise _Stop("PARTIAL", "OVERLAP_MISMATCH", f"前回の最後のページを取り直したところ、ASINの重なりが{ratio:.0%}しかありません。ページ送りの位置を信頼できません")
        visited.add(ov.url)
    elif in_resume and claim.last_page_url:
        visited.add(claim.last_page_url)
    max_pages = min(limits.max_pages, _ABSOLUTE_PAGE_LIMIT)

    def switch(next_band):
        nonlocal act, target, page_no, prev_sig, cur_pages
        act, target, page_no, prev_sig, cur_pages = next_band, bs.url(next_band), 1, None, 0

    while True:
        page = _get(service, fetcher, claim, target, limits, sleep, hb, report.pages)
        if page.url in visited:
            raise _Stop("PARTIAL", "REVISITED_URL", f"同じURLに戻りました: {page.url}")
        visited.add(page.url)
        cards = extract_search_card_asins(page.html)
        if not cards:
            if cur_pages > 0:
                raise _Stop("PARTIAL", "EMPTY_PAGE_AFTER_RESULTS", "結果の途中で0件のページになりました（想定外）")
            widget = html_has_no_results_widget(page.html)
            if widget and src.source_type in VERIFIED_EMPTY_TYPES:  # 空の帯: 確認済みの0件。閉じて次へ
                bs.list = service.advance_band(claim, act.band_id, "DONE", close_reason="EMPTY")
                report.band_empty += 1
                nb = bs.next_active()
                if nb is None:
                    return _band_result(bs, report)
                switch(nb)
                continue
            code = "EMPTY_NOT_VERIFIED_FOR_TYPE" if widget else "NO_CARDS_NO_WIDGET"
            saved_any = sum(b.pages_fetched for b in bs.list) > 0
            raise _Stop("PARTIAL" if saved_any else "UNVERIFIED_EMPTY", code, "帯の1ページ目が0件に見えますが、0件と確認できません（成功に数えません）")
        sig = _signature(cards)
        if sig == prev_sig:
            # 保存済みのページの直後に同じ内容が返った: 同じURLを1回だけ取り直して確認し、同じなら**この帯**の深さの限界
            page = _get(service, fetcher, claim, target, limits, sleep, hb, report.pages)
            report.repeat_check_pages += 1
            cards = extract_search_card_asins(page.html)
            if not cards:
                raise _Stop("PARTIAL", "EMPTY_PAGE_AFTER_RESULTS", "繰り返しの確認で取り直したら、0件のページになりました（想定外）")
            sig = _signature(cards)
            if sig == prev_sig:
                bs.close_by_depth(act, report)
                nb = bs.next_active()  # 分割なら左の子、TRUNCATED_MINなら次の待機帯
                if nb is None:
                    return _band_result(bs, report)
                switch(nb)
                continue
            # 取り直したら内容が変わっていた（一時的な不具合）: 通常のページとして処理を続ける
        prev_sig = sig
        new = [a for a in cards if a not in seen]
        seen.update(new)
        nxt = find_next_url(page.html, page.url)
        cur_pages += 1
        if nxt:
            cursor = PageCursor(nxt, page_no + 1, page.url, sig, tuple(cards), end_reached=False)
        else:
            n_end = cfg.treat_end_as_depth_limit_pages
            sp = bs.split_of(act) if (n_end is not None and cur_pages >= n_end) else None
            later = bs.pending()
            if sp is not None:  # 末尾を限界として扱い、二分割する。続きは左の子の1ページ目（再開時もDBの帯から同じ結論になる）
                cursor = PageCursor(strat.url(base_url, sp[0]), 1, page.url, sig, tuple(cards), end_reached=False)
            elif later:
                cursor = PageCursor(bs.url(later[0]), 1, page.url, sig, tuple(cards), end_reached=False)
            else:
                cursor = PageCursor(None, None, page.url, sig, tuple(cards), end_reached=True)
        service.append_asins(claim, new, pages=1, cursor=cursor, band_id=act.band_id)  # ASIN・再開位置・帯の件数を同じトランザクションで保存
        report.pages += 1
        report.asins += len(new)
        page_no += 1
        if nxt:
            if report.pages >= max_pages:
                return "SUCCESS_TRUNCATED", "PAGE_LIMIT"
            if limits.max_asins is not None and report.asins >= limits.max_asins:
                return "SUCCESS_TRUNCATED", "ASIN_LIMIT"
            target = nxt
            continue
        bs.close_at_end(act, cur_pages, report)
        if bs.active() is None and not bs.pending():
            return _band_result(bs, report)
        if report.pages >= max_pages:
            return "SUCCESS_TRUNCATED", "PAGE_LIMIT"
        if limits.max_asins is not None and report.asins >= limits.max_asins:
            return "SUCCESS_TRUNCATED", "ASIN_LIMIT"
        switch(bs.next_active())


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
            resp = self._page.goto(url, wait_until="domcontentloaded")
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
            self._page.goto(f"https://{DOMAIN[marketplace]}/", wait_until="domcontentloaded")
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
        if report.outcome in ("LEASE_LOST", "DAILY_LIMIT"):  # 日次枠が尽きた: 残りも開けない（失敗には数えない）
            break
        streak = 0 if report.outcome.startswith("SUCCESS") else streak + 1
        if streak >= stop_after_failures:
            break
    return items
