"""起点ASINからの出典（セラー・ブランド）の発見と、反対側市場への展開の記録。

流れ（DB側。ページの取得・解析は asin_collector.product_page / discovery_runner）:
1. ACTIVEレポートを`import_id`で明示し、注文点数順（既存の候補ビュー）で起点ASINを選ぶ（件数上限つき）。
2. 起点ASINの**商品ページを取得する市場(page_market)を明示**して、その商品を扱うセラーとブランドを抽出する。
3. 自社セラー（`marketplace_account_config`のis_active）と、販売元でない識別子（市場ID）を除く。**自社IDが未設定の市場では
   実行しない**。
4. 見つけた出典を`acquisition_source`へ登録し、経路を`acquisition_path`へ残す（起点import・ASIN・市場・方向、
   抽出元とセラー範囲はnoteのJSON）。同じ出典を複数起点から見つけても出典は1つで、経路だけが増える。
5. 取得済みの出典が持つASINから、**反対側市場**の商品ページを調べて同様に登録する（VIA_SOURCE経路。起点の
   指標は、今回**明示したACTIVEのimportに属する**元の出典の代表起点からコピーし、順位を引き継ぐ）。同じASINが
   反対側市場にもあるとは仮定しない。
6. 実際の出典取得は既存のplan・claim・取得間隔を通す（ここでは取得しない・迂回しない）。

**商品ページ調査の実行管理（`AsinPageGuard`）**: 出典種別に「ASINページ」は無い（CHECK制約）ため、調査1回を`collector_run`の
行（run_type='ASIN_PAGE', target='ASIN:<ASIN>', source_id無し）で管理する。**既存claimの保護はsource_idのある行にしか効かない**
ので、同等の保護をこのクラスがアプリ層で行う: 行ロック(FOR UPDATE)→トークン→状態→DB時刻のlease、の順で全ての書き込み
（heartbeat・CAPTCHA待機・終了・中断）を検証し、期限切れの回収と人による中断を持つ。回収・終了済みの行に旧プロセスが書こうと
すると`LeaseLost`。**DBトリガーによる強制は無い**（保護トリガーはsource_id付きの行だけが対象。スキーマ変更案はdocs/DEVIATIONS.md #59）。
連続失敗は出典と同じ意味: 失敗（FAILED）が連続3回で自動停止、解除は`release`（明示的な操作・理由必須・履歴の行を残す）まで続く。
中断・lease失効は失敗に数えない。成功（COMPLETED）または解除で数え直し。失敗後は60分の冷却。

**同じページの再利用**: 調査は「ページ単位」（targetにrouteを含めない）。抽出結果（出品者・ブランド・範囲）を30日間は
`collector_run.error_message`にJSONで保存し、後からSELLER→BRANDのように別のrouteを求められたら、ページへ再アクセスせずに
保存済みの結果から登録する（自社・Amazon等の除外は登録時に現在の設定で行う）。旧履歴の走査(legacy_scan)はASIN_PAGEを除外。
"""
from __future__ import annotations

import getpass
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

from sqlalchemy import text

from acquisition.lease import LeaseLost
from acquisition.locks import identity_lock
from acquisition.plan import validate_origin_imports
from acquisition.registry import SourceRegistry
from asin_collector.product_page import ProductPageResult, product_url, result_from_json
from db.session import SessionFactory
from origin_selection.candidates import Candidate, fetch_candidates
from origin_selection.report_import import ReportImportService

RUN_TYPE = "ASIN_PAGE"
ROUTES = ("SELLER", "BRAND")
RELEASE_CODE = "RELEASE_HOLD"
# 取得に使えると認める過去の実行結果（PARTIAL=失敗扱い・UNVERIFIED_EMPTY・FAILEDは使わない）。
USABLE_FETCH_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")
HARD_CAPS = {"max_origin_asins": 10, "max_sources_per_origin": 10, "max_cross_asins": 5, "max_cross_sources_per_asin": 5}

# Amazon小売かどうかを判定できない出品者は、**登録を保留して根拠を報告する**（除外とも登録とも決めつけない）。
# 根拠の種類（`Skipped.detail`に残す）:
#  - 市場IDとの一致: US `ATVPDKIKX0DER` は利用者が実ブラウザで確認したストアフロントURLの `marketplaceID=` と同じ値、
#    JP `A1VC38T7YXB528` はJPの市場IDとして知られる値。**これらが販売元識別子としても使われ得ないとは断定しない**
#    （リポジトリ・DBに「Amazon自身の販売元ID」としての根拠が無いことと、そうでないことは別）。DBの既存記録
#    （known_seller_master・storefront_blocked_seller）ではセラーとして記録されている。確認できるまで登録しない。
#  - 表示名がAmazon小売と完全一致（amazon / amazon.com / amazon.co.jp / amazon.com services llc）。
# 配送元の表示（Ships from）は出典の候補にしない。FBAの出品者（配送元がAmazonなだけ）は、販売元リンクのIDが別なので登録する。
# 確認できないIDを推測で追加しない。判定は人が行い、販売元がAmazon小売なら storefront_blocked_seller へ（このコードは変更しない）。
AMAZON_RETAIL_UNDETERMINED_IDS = {"US": {"ATVPDKIKX0DER"}, "JP": {"A1VC38T7YXB528"}}
AMAZON_RETAIL_NAMES = {"amazon", "amazon.com", "amazon.co.jp", "amazon.com services llc"}
HELD_AMAZON = "HELD_AMAZON_RETAIL_UNDETERMINED"


class DiscoveryError(Exception):
    """設定不足・指定の誤りなどで、発見を始められない。"""


class InvestigationBlocked(Exception):
    def __init__(self, state: str, until: datetime | None = None):
        self.state, self.until = state, until
        super().__init__(f"{state}" + (f"（{until:%Y-%m-%d %H:%M} UTCまで）" if until else ""))


@dataclass
class DiscoveryLimits:
    """展開数の上限。起点ASIN数・起点ごとの出典数・反対側市場への展開数をそれぞれ制限する。"""
    max_origin_asins: int = 2
    max_sources_per_origin: int = 3
    max_cross_asins: int = 2
    max_cross_sources_per_asin: int = 3

    def validate(self) -> "DiscoveryLimits":
        for name, cap in HARD_CAPS.items():
            v = getattr(self, name)
            if not isinstance(v, int) or v < 1 or v > cap:
                raise DiscoveryError(f"{name} は1〜{cap}で指定してください（指定: {v}）")
        return self


def routes_tag(routes: Iterable[str]) -> str:
    rs = sorted(set(routes))
    if not rs or any(r not in ROUTES for r in rs):
        raise DiscoveryError(f"routes は {ROUTES} から指定してください")
    return "+".join(rs)


def investigation_target(asin: str) -> str:
    return f"ASIN:{asin.strip().upper()}"


# ---------------------------------------------------------------------- 実行管理（lease付き・出典と同じ意味の自動停止）

@dataclass(frozen=True)
class GuardConfig:
    revisit_days: int = 30
    cooldown_minutes: int = 60
    max_failures: int = 3
    lease_seconds: int = 300
    captcha_lease_seconds: int = 1800


@dataclass(frozen=True)
class InvestigationRow:
    run_id: int
    status: str
    outcome: str | None
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    lease_expires_at: datetime | None
    detail: str | None = None


@dataclass
class PageLookup:
    state: str  # ELIGIBLE / REUSABLE / RUNNING / COOLDOWN / AUTO_STOPPED / TOO_RECENT
    until: datetime | None = None
    run_id: int | None = None
    stored: ProductPageResult | None = None
    failures: int = 0


@dataclass(frozen=True)
class PageClaim:
    run_id: int
    token: str
    market: str
    asin: str
    lease_seconds: int
    captcha_lease_seconds: int


def decide_investigation(rows: list[InvestigationRow], now: datetime, cfg: GuardConfig = GuardConfig()) -> PageLookup:
    """同じページの過去の調査行から、今どうするかを判定する（純粋関数）。
    ELIGIBLE=調べてよい / REUSABLE=30日以内の保存済み結果を再利用できる（アクセス不要） / それ以外は不可。
    期限切れのRUNNINGは実行中に数えない（回収対象）。中断(INTERRUPTED)・lease失効は失敗に数えない。"""
    for r in rows:
        if r.status in ("RUNNING", "CAPTCHA_WAIT") and r.lease_expires_at is not None and r.lease_expires_at > now:
            return PageLookup("RUNNING", r.lease_expires_at, r.run_id)
    done = [r for r in rows if r.status == "COMPLETED" and r.completed_at]
    last_done = max(done, key=lambda r: r.completed_at, default=None)
    releases = [r.completed_at for r in rows if r.error_code == RELEASE_CODE and r.completed_at]
    cut = max([t for t in ([last_done.completed_at] if last_done else []) + releases], default=None)
    if last_done is not None and now < last_done.completed_at + timedelta(days=cfg.revisit_days):
        due = last_done.completed_at + timedelta(days=cfg.revisit_days)
        if last_done.outcome == "SUCCESS_NONEMPTY" and result_from_json(last_done.detail) is not None:
            return PageLookup("REUSABLE", due, last_done.run_id, result_from_json(last_done.detail))
        return PageLookup("TOO_RECENT", due, last_done.run_id)  # 該当なし等は再調査しない（再利用できる結果も無い）
    fails = [r for r in rows if r.status == "FAILED" and r.completed_at and (cut is None or r.completed_at > cut)]
    if fails:
        last_fail = max(f.completed_at for f in fails)
        if len(fails) >= cfg.max_failures:
            return PageLookup("AUTO_STOPPED", None, None, failures=len(fails))  # 解除(release)まで続く
        if now < last_fail + timedelta(minutes=cfg.cooldown_minutes):
            return PageLookup("COOLDOWN", last_fail + timedelta(minutes=cfg.cooldown_minutes), None, failures=len(fails))
    return PageLookup("ELIGIBLE", failures=len(fails))


# 調査の結果 → collector_run の (status, outcome, error_code)。実ページで未確認の0件は成功にしない。
_FINISH = {
    "FOUND": ("COMPLETED", "SUCCESS_NONEMPTY", None),
    "FOUND_NO_DATA": ("FAILED", "UNVERIFIED_EMPTY", "FOUND_NO_DATA"),
    "NOT_FOUND": ("COMPLETED", "SUCCESS_EMPTY", "ASIN_NOT_FOUND"),
    "UNCONFIRMED": ("FAILED", "UNVERIFIED_EMPTY", "PAGE_UNCONFIRMED"),
    "LOAD_FAILED": ("FAILED", "FAILED", "PAGE_LOAD_FAILED"),
    "SERVER_ERROR": ("FAILED", "FAILED", "SERVER_ERROR"),
    "CAPTCHA_UNSOLVED": ("FAILED", "FAILED", "CAPTCHA_UNSOLVED"),
    "REGISTER_ERROR": ("FAILED", "FAILED", "REGISTER_ERROR"),
}


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class AsinPageGuard:
    def __init__(self, app: SessionFactory, cfg: GuardConfig = GuardConfig(), actor: str | None = None) -> None:
        self._app, self._cfg, self._actor = app, cfg, actor or _actor()

    # ---- 読み取り
    @staticmethod
    def _rows(conn, market: str, target: str) -> tuple[list[InvestigationRow], datetime]:
        rows = [
            InvestigationRow(r.id, r.status, r.outcome, r.error_code, r.started_at, r.completed_at, r.lease_expires_at, r.error_message)
            for r in conn.execute(
                text("SELECT id, status, outcome, error_code, started_at, completed_at, lease_expires_at, error_message "
                     "FROM collector_run WHERE run_type = :rt AND marketplace = :m AND target = :t AND source_id IS NULL ORDER BY id"),
                {"rt": RUN_TYPE, "m": market, "t": target},
            )
        ]
        return rows, conn.execute(text("SELECT clock_timestamp()")).scalar_one()

    def lookup(self, market: str, asin: str) -> PageLookup:
        """読み取りだけ（記録しない）。"""
        with self._app.get_session(market) as conn, conn.begin():
            rows, now = self._rows(conn, market, investigation_target(asin))
        return decide_investigation(rows, now, self._cfg)

    # ---- 開始（ロック下で再判定。期限切れの実行は回収してから）
    def begin(self, market: str, asin: str) -> PageClaim:
        target = investigation_target(asin)
        token = str(uuid.uuid4())
        with self._app.get_session(market) as conn, conn.begin():
            identity_lock(conn, market, RUN_TYPE, target)
            conn.execute(
                text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = 'LEASE_EXPIRED', "
                     "completed_at = clock_timestamp(), error_message = 'lease expired (reaped by begin)' WHERE run_type = :rt "
                     "AND marketplace = :m AND target = :t AND source_id IS NULL AND status IN ('RUNNING','CAPTCHA_WAIT') "
                     "AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())"),
                {"rt": RUN_TYPE, "m": market, "t": target},
            )
            rows, now = self._rows(conn, market, target)
            lk = decide_investigation(rows, now, self._cfg)
            if lk.state != "ELIGIBLE":
                raise InvestigationBlocked(lk.state, lk.until)
            run_id = conn.execute(
                text("INSERT INTO collector_run (marketplace, run_type, target, status, attempt_token, heartbeat_at, lease_expires_at, "
                     "lease_seconds) VALUES (:m, :rt, :t, 'RUNNING', CAST(:tok AS uuid), clock_timestamp(), "
                     "clock_timestamp() + make_interval(secs => :ls), :ls) RETURNING id"),
                {"m": market, "rt": RUN_TYPE, "t": target, "tok": token, "ls": self._cfg.lease_seconds},
            ).scalar_one()
        return PageClaim(run_id, token, market, asin.strip().upper(), self._cfg.lease_seconds, self._cfg.captcha_lease_seconds)

    # ---- 実行中の操作（全て 行ロック→トークン→状態→DB時刻のlease の順で検証）
    @staticmethod
    def _lock_valid(conn, claim: PageClaim, allowed=("RUNNING", "CAPTCHA_WAIT")):
        row = conn.execute(
            text("SELECT id, status, attempt_token, (lease_expires_at > clock_timestamp()) AS lease_ok FROM collector_run "
                 "WHERE id = :id AND run_type = :rt AND source_id IS NULL FOR UPDATE"),
            {"id": claim.run_id, "rt": RUN_TYPE},
        ).one_or_none()
        if row is None:
            raise LeaseLost(f"ASIN_PAGE run={claim.run_id} が存在しません")
        if row.attempt_token is None or str(row.attempt_token) != claim.token:
            raise LeaseLost(f"ASIN_PAGE run={claim.run_id}: トークンが一致しません")
        if row.status not in allowed:
            raise LeaseLost(f"ASIN_PAGE run={claim.run_id}: 状態が{row.status}です（回収済みまたは終了済み）")
        if not row.lease_ok:
            raise LeaseLost(f"ASIN_PAGE run={claim.run_id}: leaseが失効しています（DB時刻）")
        return row

    def _extend(self, claim: PageClaim, status_after: str | None, allowed, secs: int) -> None:
        with self._app.get_session(claim.market) as conn, conn.begin():
            self._lock_valid(conn, claim, allowed=allowed)
            conn.execute(
                text("UPDATE collector_run SET status = COALESCE(:st, status), heartbeat_at = clock_timestamp(), "
                     "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"),
                {"st": status_after, "ls": secs, "id": claim.run_id},
            )

    def heartbeat(self, claim: PageClaim) -> None:
        """leaseを延長する。失効後は復活させない（LeaseLost）。"""
        with self._app.get_session(claim.market) as conn, conn.begin():
            row = self._lock_valid(conn, claim)
            secs = claim.captcha_lease_seconds if row.status == "CAPTCHA_WAIT" else claim.lease_seconds
            conn.execute(
                text("UPDATE collector_run SET heartbeat_at = clock_timestamp(), "
                     "lease_expires_at = clock_timestamp() + make_interval(secs => :ls) WHERE id = :id"),
                {"ls": secs, "id": claim.run_id},
            )

    def enter_captcha_wait(self, claim: PageClaim) -> None:
        self._extend(claim, "CAPTCHA_WAIT", ("RUNNING",), claim.captcha_lease_seconds)

    def leave_captcha_wait(self, claim: PageClaim) -> None:
        self._extend(claim, "RUNNING", ("CAPTCHA_WAIT",), claim.lease_seconds)

    def assert_valid(self, claim: PageClaim) -> None:
        """書き込み（出典・経路の登録）の直前に呼ぶ。leaseを失っていれば LeaseLost。"""
        with self._app.get_session(claim.market) as conn, conn.begin():
            self._lock_valid(conn, claim)

    def finish(self, claim: PageClaim, kind: str, *, candidates: int = 0, detail: str | None = None) -> None:
        """終了確定。leaseが失効・回収済みなら LeaseLost（何も書かない）。FOUNDのdetailは抽出結果JSON。"""
        status, outcome, code = _FINISH[kind]
        with self._app.get_session(claim.market) as conn, conn.begin():
            identity_lock(conn, claim.market, RUN_TYPE, investigation_target(claim.asin))
            self._lock_valid(conn, claim)
            conn.execute(
                text("UPDATE collector_run SET status = :s, outcome = :o, error_code = :c, error_message = :d, asin_count = :n, "
                     "completed_at = clock_timestamp(), heartbeat_at = clock_timestamp() WHERE id = :id"),
                {"s": status, "o": outcome, "c": code, "d": detail, "n": candidates, "id": claim.run_id},
            )

    def interrupt(self, claim: PageClaim, kind: str = "USER", message: str | None = None) -> bool:
        """実行者自身の中断（Ctrl+C）。leaseを失っていれば何もしない（False）。失敗に数えない。
        DAILY_LIMIT: 日次のページ枠が尽きてページを開かなかった（失敗・冷却・自動停止に数えない。翌JST日付に調べ直せる）。"""
        if kind not in ("USER", "DAILY_LIMIT"):
            raise ValueError("kind must be USER or DAILY_LIMIT")
        try:
            with self._app.get_session(claim.market) as conn, conn.begin():
                self._lock_valid(conn, claim)
                conn.execute(
                    text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = :k, "
                         "completed_at = clock_timestamp(), error_message = coalesce(:m, error_message) WHERE id = :id"),
                    {"id": claim.run_id, "k": kind, "m": message[:500] if message else None},
                )
            return True
        except LeaseLost:
            return False

    # ---- 人による操作・回収
    def abort_run(self, market: str, run_id: int, reason: str) -> None:
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        with self._app.get_session(market) as conn, conn.begin():
            row = conn.execute(
                text("SELECT status FROM collector_run WHERE id = :id AND run_type = :rt AND source_id IS NULL FOR UPDATE"),
                {"id": run_id, "rt": RUN_TYPE},
            ).one_or_none()
            if row is None:
                raise ValueError(f"run={run_id} は商品ページ調査として存在しません")
            if row.status not in ("RUNNING", "CAPTCHA_WAIT"):
                raise ValueError(f"run={run_id} は既に終了しています（{row.status}）")
            conn.execute(
                text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = 'USER_ABORT', "
                     "completed_at = clock_timestamp(), error_message = :m WHERE id = :id"),
                {"m": f"abort by {self._actor}: {reason.strip()}", "id": run_id},
            )

    def reap_expired(self, market: str) -> list[int]:
        """期限切れ（DB時刻）の実行を INTERRUPTED(LEASE_EXPIRED) に確定する。行ごとにロックし、ロック後に期限を再評価する。"""
        with self._app.get_session(market) as conn, conn.begin():
            ids = [r.id for r in conn.execute(
                text("SELECT id FROM collector_run WHERE run_type = :rt AND source_id IS NULL AND status IN ('RUNNING','CAPTCHA_WAIT') "
                     "AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()) ORDER BY id"), {"rt": RUN_TYPE})]
        reaped: list[int] = []
        for run_id in ids:
            with self._app.get_session(market) as conn, conn.begin():
                got = conn.execute(
                    text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = 'LEASE_EXPIRED', "
                         "completed_at = clock_timestamp(), error_message = 'lease expired (reaped)' WHERE id = :id AND run_type = :rt "
                         "AND source_id IS NULL AND status IN ('RUNNING','CAPTCHA_WAIT') "
                         "AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()) RETURNING id"),
                    {"id": run_id, "rt": RUN_TYPE},
                ).one_or_none()
                if got is not None:
                    reaped.append(got.id)
        return reaped

    def release(self, market: str, asin: str, reason: str) -> None:
        """自動停止（連続失敗）を解除する。理由必須。以降それ以前の失敗は数えない（履歴の行は残る）。"""
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        target = investigation_target(asin)
        with self._app.get_session(market) as conn, conn.begin():
            identity_lock(conn, market, RUN_TYPE, target)
            rows, now = self._rows(conn, market, target)
            lk = decide_investigation(rows, now, self._cfg)
            if lk.state not in ("AUTO_STOPPED", "COOLDOWN"):
                raise ValueError(f"解除する停止がありません（状態: {lk.state}）")
            conn.execute(
                text("INSERT INTO collector_run (marketplace, run_type, target, status, outcome, interrupt_kind, error_code, error_message, "
                     "completed_at) VALUES (:m, :rt, :t, 'INTERRUPTED', 'INTERRUPTED', 'USER_ABORT', :c, :msg, clock_timestamp())"),
                {"m": market, "rt": RUN_TYPE, "t": target, "c": RELEASE_CODE, "msg": f"release by {self._actor}: {reason.strip()}"},
            )


# ---------------------------------------------------------------------- 自社ID

def load_own_account_ids(app: SessionFactory, market: str) -> set[str]:
    with app.get_session(market) as conn, conn.begin():
        return {r[0].strip().upper() for r in conn.execute(
            text("SELECT account_id FROM marketplace_account_config WHERE marketplace = :m AND is_active"), {"m": market})}


def require_own_accounts(app: SessionFactory, market: str) -> set[str]:
    ids = load_own_account_ids(app, market)
    if not ids:
        raise DiscoveryError(
            f"市場{market}の自社セラーIDが未設定です（`acquisition.cli own-account-add --market {market} --account-id <ID>` で登録してください）。"
            "自社を除外できないため、自動の発見は実行しません")
    return ids


# ---------------------------------------------------------------------- 候補の組み立て・登録

@dataclass(frozen=True)
class SourceCandidate:
    source_type: str
    key: str
    meta: dict


@dataclass
class Skipped:
    key: str
    reason: str
    detail: str | None = None  # 見送り・保留の根拠（報告用）


def build_candidates(result, routes: Iterable[str], page_market: str, own_ids: set[str], limit: int,
                     host: str) -> tuple[list[SourceCandidate], list[Skipped]]:
    """商品ページの抽出結果から、登録する候補を上限つきで選ぶ（ブランド→セラーの順）。
    除外: 自社(OWN_SELLER)。保留（登録せず根拠を報告）: Amazon小売かを判定できないもの(HELD_AMAZON_RETAIL_UNDETERMINED)。
    配送元の表示は候補にならない。配送元がAmazonなだけのFBA出品者は、販売元リンクのIDが別なので登録する。"""
    from urllib.parse import urljoin

    routes = set(routes)
    cands: list[SourceCandidate] = []
    skipped: list[Skipped] = []
    if "BRAND" in routes:
        for b in result.brands:
            key = urljoin(f"https://{host}/", b.key) if b.source_type == "BRAND_STORE" else b.key
            cands.append(SourceCandidate(b.source_type, key, {"basis": b.basis}))
    if "SELLER" in routes:
        for s in result.sellers:
            basis = f"リンクの役割={s.role} 領域={s.region} 表示名={s.name!r}"
            if s.seller_id in own_ids:
                skipped.append(Skipped(s.seller_id, "OWN_SELLER", basis))
            elif s.seller_id in AMAZON_RETAIL_UNDETERMINED_IDS.get(page_market, set()):
                skipped.append(Skipped(s.seller_id, HELD_AMAZON, f"市場{page_market}の市場IDと一致（販売元識別子かは未確認）; {basis}"))
            elif (s.name or "").strip().casefold() in AMAZON_RETAIL_NAMES:
                skipped.append(Skipped(s.seller_id, HELD_AMAZON, f"表示名がAmazon小売と完全一致（IDからは判定不能）; {basis}"))
            else:
                cands.append(SourceCandidate("SELLER", s.seller_id, {"region": s.region, "seller_scope": result.seller_scope,
                                                                      "name": s.name}))
    for c in cands[limit:]:
        skipped.append(Skipped(c.key, "OVER_LIMIT"))
    return cands[:limit], skipped


@dataclass
class Registered:
    source_id: int
    source_type: str
    key: str
    source_created: bool
    path_created: bool


@dataclass
class Pick:
    asin: str
    mode: str  # FETCH（ページを取得する）/ REUSE（保存済みの調査結果を使う。ページへアクセスしない）
    candidate: Candidate | None = None
    stored: ProductPageResult | None = None
    stored_run_id: int | None = None


SELECTION_MODES = ("ordered_only", "ordered_then_zero", "zero_only")


def rank_candidates(cands: list[Candidate], mode: str) -> list[Candidate]:
    """候補の探索順（レポートのimport単位の集計値のみ。必須の足切りは無い）。
    注文あり: 注文点数→セッション数→最大獲得率の降順。注文0点: セッション数→最大獲得率の降順。同点はASIN順で安定。
    ordered_only=注文ありのみ / ordered_then_zero=注文あり→注文0点 / zero_only=注文0点のみ。"""
    if mode not in SELECTION_MODES:
        raise DiscoveryError(f"selection_mode は {SELECTION_MODES} から指定してください: {mode!r}")
    ordered = sorted((c for c in cands if c.units_sum > 0), key=lambda c: (-c.units_sum, -c.sessions_sum, -float(c.buybox_rate_max_pct), c.asin))
    zero = sorted((c for c in cands if c.units_sum <= 0), key=lambda c: (-c.sessions_sum, -float(c.buybox_rate_max_pct), c.asin))
    return {"ordered_only": ordered, "ordered_then_zero": ordered + zero, "zero_only": zero}[mode]


@dataclass
class OriginSelection:
    selected: list[Pick]
    skipped: list[tuple[str, str, datetime | None]] = field(default_factory=list)  # (asin, state, until)
    origin_market: str | None = None
    done: list[tuple[str, datetime | None]] = field(default_factory=list)  # 探索済み（未処理の続きが無い）と再探索可能時刻
    scanned: int = 0  # 今回の選択で調べた候補数（上位から）
    mode: str | None = None


@dataclass
class CrossSelection:
    picks: list[Pick]
    skipped: list[tuple[str, str, datetime | None]]
    run_id: int
    run_outcome: str
    run_completed_at: datetime
    run_age_days: float
    truncated: bool
    origin_asins: tuple[str, ...] = ()  # 元の起点ASIN（展開の対象から除く）


class DiscoveryService:
    def __init__(self, app: SessionFactory, guard: AsinPageGuard | None = None, actor: str | None = None) -> None:
        self._app = app
        self.guard = guard or AsinPageGuard(app, actor=actor)
        self._registry = SourceRegistry(app, actor=actor)
        self._reports = ReportImportService(app, None, actor=actor)

    def _pick(self, market: str, asin: str, candidate: Candidate | None = None) -> tuple[Pick | None, tuple[str, str, datetime | None] | None]:
        lk = self.guard.lookup(market, asin)
        if lk.state == "ELIGIBLE":
            return Pick(asin, "FETCH", candidate), None
        if lk.state == "REUSABLE":
            return Pick(asin, "REUSE", candidate, lk.stored, lk.run_id), None
        return None, (asin, lk.state, lk.until)

    # ---- 起点の選択
    def select_origins(self, origin_import_id: int, page_market: str, limits: DiscoveryLimits, include_zero_units: bool = False,
                       after_rank: int = 0, *, mode: str | None = None,
                       has_pending: Callable[[Candidate, ProductPageResult | None], bool] | None = None) -> OriginSelection:
        """mode=None: 従来の選択（レポート順位・include_zero_units）。modeを指定すると、候補を`rank_candidates`の順に調べ、
        ①未調査/再調査可能(ELIGIBLE)→取得 ②30日内に調査済み(REUSABLE)→**未処理の続き(has_pending)があれば**保存済み結果で続行、
        無ければ「探索済み」として枠を使わず次へ ③実行中・冷却中・自動停止・該当なし→飛ばして次へ（上位の保留が他の候補を塞がない）。
        配列位置のカーソルは持たず、毎回この履歴（調査行・経路・投入台帳）から決まる。"""
        limits.validate()
        info = validate_origin_imports(self._reports, [origin_import_id])[0]
        with self._app.get_session(info.marketplace) as conn, conn.begin():
            ranked = fetch_candidates(conn, origin_import_id)
        if mode is not None:
            sel = OriginSelection([], origin_market=info.marketplace, mode=mode)
            for c in rank_candidates(ranked, mode):
                if c.rank <= after_rank:
                    continue
                sel.scanned += 1
                lk = self.guard.lookup(page_market, c.asin)
                if lk.state == "ELIGIBLE":
                    sel.selected.append(Pick(c.asin, "FETCH", c))
                elif lk.state == "REUSABLE":
                    if has_pending is not None and has_pending(c, lk.stored):
                        sel.selected.append(Pick(c.asin, "REUSE", c, lk.stored, lk.run_id))  # 外部再取得せず、保存済みの結果から続きを処理
                    else:
                        sel.done.append((c.asin, lk.until))
                else:
                    sel.skipped.append((c.asin, lk.state, lk.until))
                if len(sel.selected) >= limits.max_origin_asins:
                    break
            return sel
        sel = OriginSelection([], origin_market=info.marketplace)
        for c in ranked:
            if c.rank <= after_rank:
                continue
            if c.units_sum <= 0 and not include_zero_units:
                continue  # 注文0点の候補は(経路としては残るが)既定では調べない
            pick, skip = self._pick(page_market, c.asin, c)
            if skip:
                sel.skipped.append(skip)
                continue
            sel.selected.append(pick)
            if len(sel.selected) >= limits.max_origin_asins:
                break
        return sel

    # ---- 起点ASINの発見結果の登録
    def record_origin(self, origin: Candidate, page_market: str, result: ProductPageResult, routes: Iterable[str],
                      own_ids: set[str], limits: DiscoveryLimits, run_id: int,
                      check: Callable[[], None] | None = None) -> tuple[list[Registered], list[Skipped]]:
        page_url = product_url(page_market, origin.asin)
        cands, skipped = build_candidates(result, routes, page_market, own_ids, limits.max_sources_per_origin, page_url.split("/")[2])
        out: list[Registered] = []
        for c in cands:
            if check:
                check()  # leaseを失っていれば書き込まない（LeaseLost）
            note = json.dumps({"v": 1, "route": "ORIGIN_PRODUCT_PAGE", "extracted_from": "PRODUCT_PAGE", "page_market": page_market,
                               "page_asin": origin.asin, "page_url": page_url, "investigation_run": run_id,
                               "routes": sorted(set(routes)), **c.meta}, ensure_ascii=False)[:900]
            rp = self._registry.register_path(page_market, c.source_type, c.key, "", origin_import_id=origin.import_id,
                                              origin_asin=origin.asin, note=note)
            out.append(Registered(rp.source.source_id, c.source_type, c.key, rp.source.created, rp.path_created))
        return out, skipped

    # ---- 反対側市場への展開
    def select_cross_asins(self, via_market: str, via_source_id: int, page_market: str, limits: DiscoveryLimits,
                           origin_import_ids: list[int], *, max_run_age_days: int = 30, run_id: int | None = None) -> CrossSelection:
        """取得済みの出典(via_market, via_source_id)の**過去の取得結果**のASINのうち、反対側市場page_marketで調べてよいもの。
        使う実行は、COMPLETEDで結果が USABLE_FETCH_OUTCOMES（成功・上限打切り）のものだけ。出典の再取得はしない（再取得は
        collect-source。30日間隔を通る）。起点は`origin_import_ids`（今回指定したACTIVEのimport）に属する経路に限る。"""
        limits.validate()
        if via_market == page_market:
            raise DiscoveryError("反対側の市場を指定してください（元の出典の市場と商品ページの市場が同じです）")
        if not origin_import_ids:
            raise DiscoveryError("起点のimport_id（ACTIVE）を --origin-import で明示してください（過去レポートの経路は自動では選びません）")
        validate_origin_imports(self._reports, origin_import_ids)
        with self._app.get_session(via_market) as conn, conn.begin():
            src = conn.execute(text("SELECT source_id FROM acquisition_source WHERE source_id = :s"), {"s": via_source_id}).one_or_none()
            if src is None:
                raise DiscoveryError(f"source_id={via_source_id} は市場{via_market}に存在しません")
            n_paths = conn.execute(
                text("SELECT count(*) FROM acquisition_path WHERE source_id = :s AND stage = 'ORIGIN_ASIN' AND origin_import_id = ANY(:ids)"),
                {"s": via_source_id, "ids": origin_import_ids}).scalar_one()
            if n_paths == 0:
                raise DiscoveryError(f"source_id={via_source_id} には、指定したimport {origin_import_ids} の起点経路がありません"
                                     "（別のレポートの経路は使いません）")
            params = {"s": via_source_id, "ok": list(USABLE_FETCH_OUTCOMES)}
            sql = ("SELECT id, outcome, completed_at, extract(epoch FROM clock_timestamp() - completed_at) / 86400.0 AS age_days "
                   "FROM collector_run WHERE source_id = :s AND status = 'COMPLETED' AND outcome = ANY(:ok)")
            if run_id is not None:
                sql += " AND id = :rid"
                params["rid"] = run_id
            run = conn.execute(text(sql + " ORDER BY completed_at DESC, id DESC LIMIT 1"), params).one_or_none()
            if run is None:
                raise DiscoveryError(
                    f"source_id={via_source_id} には、利用できる取得結果（結果が {', '.join(USABLE_FETCH_OUTCOMES)} の完了した実行）が"
                    "ありません。先に collect-source で取得してください（30日間隔を通ります）")
            if run.age_days > max_run_age_days:
                raise DiscoveryError(f"取得結果(run={run.id})は{run.age_days:.1f}日前で、--max-run-age-days={max_run_age_days}を超えています。"
                                     "再取得は collect-source（30日間隔を通ります）で行ってください")
            asins = [r[0] for r in conn.execute(
                text("SELECT asin FROM collector_asin WHERE collector_run_id = :r ORDER BY id"), {"r": run.id})]
            origin_asins = tuple(sorted(r[0] for r in conn.execute(
                text("SELECT DISTINCT origin_asin FROM acquisition_path WHERE source_id = :s AND stage = 'ORIGIN_ASIN' "
                     "AND origin_import_id = ANY(:ids)"), {"s": via_source_id, "ids": origin_import_ids})))
        picks: list[Pick] = []
        skipped: list[tuple[str, str, datetime | None]] = []
        for a in asins:  # 取得結果の保存順（collector_asin.id）。決定的
            if a in origin_asins:  # 元の起点ASINは展開の対象にしない（起点の商品ページは既に調べた市場のもの）
                skipped.append((a, "ORIGIN_ASIN", None))
                continue
            pick, skip = self._pick(page_market, a)
            if skip:
                skipped.append(skip)
                continue
            picks.append(pick)
            if len(picks) >= limits.max_cross_asins:
                break
        return CrossSelection(picks, skipped, run.id, run.outcome, run.completed_at, float(run.age_days),
                              run.outcome == "SUCCESS_TRUNCATED", origin_asins)

    def record_cross(self, via_market: str, via_source_id: int, via_asin: str, page_market: str, result: ProductPageResult,
                     routes: Iterable[str], own_ids: set[str], limits: DiscoveryLimits, run_id: int, origin_import_ids: list[int],
                     check: Callable[[], None] | None = None, *, via_run_id: int | None = None,
                     via_run_outcome: str | None = None) -> tuple[list[Registered], list[Skipped]]:
        page_url = product_url(page_market, via_asin)
        cands, skipped = build_candidates(result, routes, page_market, own_ids, limits.max_cross_sources_per_asin, page_url.split("/")[2])
        out: list[Registered] = []
        for c in cands:
            if check:
                check()
            note = json.dumps({"v": 1, "route": "CROSS_MARKET_PRODUCT_PAGE", "extracted_from": "PRODUCT_PAGE", "page_market": page_market,
                               "page_asin": via_asin, "page_url": page_url, "via_market": via_market, "investigation_run": run_id,
                               "routes": sorted(set(routes)), "asin_existence_in_page_market": "FOUND", "via_fetch_run": via_run_id,
                               "via_fetch_outcome": via_run_outcome,
                               "derived_from_partial_result": via_run_outcome == "SUCCESS_TRUNCATED", **c.meta},
                              ensure_ascii=False)[:1000]
            rp = self._registry.register_via_path(page_market, c.source_type, c.key, "", via_market=via_market,
                                                  via_source_id=via_source_id, via_asin=via_asin, note=note,
                                                  allowed_origin_import_ids=origin_import_ids)
            out.append(Registered(rp.source.source_id, c.source_type, c.key, rp.source.created, rp.path_created))
        return out, skipped
