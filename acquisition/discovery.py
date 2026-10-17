"""起点ASINからの出典（セラー・ブランド）の発見と、反対側市場への展開の記録。

流れ（DB側。ページの取得・解析は asin_collector.product_page / discovery_runner）:
1. ACTIVEレポートを`import_id`で明示し、注文点数順（既存の候補ビュー）で起点ASINを選ぶ（件数上限つき）。
2. 起点ASINの**商品ページを取得する市場(page_market)を明示**して、その商品を扱うセラー・ブランドを抽出する。
3. 自社セラー（`marketplace_account_config`のis_active）とAmazon自身を除く。**自社IDが未設定の市場では実行しない**。
4. 見つけた出典を`acquisition_source`へ登録し、経路を`acquisition_path`へ残す（起点import・ASIN・市場・方向、
   抽出元とセラー範囲はnoteのJSON）。同じ出典を複数起点から見つけても出典は1つで、経路だけが増える。
5. 取得済みの出典が持つASINから、**反対側市場**の商品ページを調べて同様に登録する（VIA_SOURCE経路。起点の
   指標は元の出典の代表起点からコピーし、順位を引き継ぐ）。同じASINが反対側市場にもあるとは仮定しない。
6. 実際の出典取得は既存のplan・claim・取得間隔を通す（ここでは取得しない・迂回しない）。

**商品ページ調査の再探索間隔（既存スキーマでの管理）**: 出典種別に「ASINページ」は無い（種別はCHECK制約で固定）ため、
調査1回を`collector_run`の行（run_type='ASIN_PAGE', target='ASIN:<ASIN>|<routes>', source_id無し）として残し、
`AsinPageGuard`が同じ対象の再調査を管理する: 完了から`revisit_days`(30日)・失敗後の冷却・連続失敗の上限・
実行中の二重起動を拒否する。出典のようなlease/heartbeatやフェンシングは無く、二重起動の防止は識別ロック＋実行中行
（stale時間つき）による。恒久的には出典種別の追加（docs/DEVIATIONS.md #59のスキーマ変更案）で他の出典と同じ
実行制御に乗せる。旧履歴の走査(legacy_scan)はこの行を除外している。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import text

from acquisition.locks import identity_lock
from acquisition.plan import validate_origin_imports
from acquisition.registry import SourceRegistry
from db.session import SessionFactory
from origin_selection.candidates import Candidate, fetch_candidates
from origin_selection.report_import import ReportImportService

RUN_TYPE = "ASIN_PAGE"
ROUTES = ("SELLER", "BRAND")
# Amazon自身（小売）の出品者ID。取得対象のセラーとして登録しない（実ページ未検証の既知の値）。
AMAZON_RETAIL_SELLER_IDS = {"US": {"ATVPDKIKX0DER"}, "JP": {"AN1VRQENFRJN5"}}
HARD_CAPS = {"max_origin_asins": 10, "max_sources_per_origin": 10, "max_cross_asins": 5, "max_cross_sources_per_asin": 5}


class DiscoveryError(Exception):
    """設定不足・指定の誤りなどで、発見を始められない。"""


class InvestigationBlocked(Exception):
    def __init__(self, state: str, until: datetime | None):
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


def investigation_target(asin: str, routes: Iterable[str]) -> str:
    return f"ASIN:{asin.strip().upper()}|{routes_tag(routes)}"


# ---------------------------------------------------------------------- 再探索間隔（商品ページ調査）

@dataclass(frozen=True)
class GuardConfig:
    revisit_days: int = 30
    cooldown_minutes: int = 60
    max_failures: int = 3
    stale_minutes: int = 30


@dataclass(frozen=True)
class InvestigationRow:
    status: str
    outcome: str | None
    started_at: datetime
    completed_at: datetime | None


def decide_investigation(rows: list[InvestigationRow], now: datetime, cfg: GuardConfig = GuardConfig()) -> tuple[str, datetime | None]:
    """同じ対象の過去の調査行から、今調べてよいかを判定する（純粋関数）。中断(INTERRUPTED)は失敗に数えない。"""
    stale = timedelta(minutes=cfg.stale_minutes)
    for r in rows:
        if r.status in ("RUNNING", "CAPTCHA_WAIT") and r.started_at + stale > now:
            return "RUNNING", r.started_at + stale
    done = [r.completed_at for r in rows if r.status == "COMPLETED" and r.completed_at]
    last_done = max(done, default=None)
    if last_done is not None and now < last_done + timedelta(days=cfg.revisit_days):
        return "TOO_RECENT", last_done + timedelta(days=cfg.revisit_days)
    fails = [r.completed_at for r in rows if r.status == "FAILED" and r.completed_at and (last_done is None or r.completed_at > last_done)]
    if fails:
        last_fail = max(fails)
        if now < last_fail + timedelta(minutes=cfg.cooldown_minutes):
            return "COOLDOWN", last_fail + timedelta(minutes=cfg.cooldown_minutes)
        if len(fails) >= cfg.max_failures and now < last_fail + timedelta(days=cfg.revisit_days):
            return "TOO_MANY_FAILURES", last_fail + timedelta(days=cfg.revisit_days)
    return "ELIGIBLE", None


# 調査の結果 → collector_run の (status, outcome, error_code)。実ページで未確認の0件は成功にしない。
_FINISH = {
    "FOUND": ("COMPLETED", "SUCCESS_NONEMPTY", None),
    "FOUND_NO_CANDIDATES": ("FAILED", "UNVERIFIED_EMPTY", "FOUND_NO_CANDIDATES"),
    "NOT_FOUND": ("COMPLETED", "SUCCESS_EMPTY", "ASIN_NOT_FOUND"),
    "UNCONFIRMED": ("FAILED", "UNVERIFIED_EMPTY", "PAGE_UNCONFIRMED"),
    "LOAD_FAILED": ("FAILED", "FAILED", "PAGE_LOAD_FAILED"),
    "SERVER_ERROR": ("FAILED", "FAILED", "SERVER_ERROR"),
    "CAPTCHA_UNSOLVED": ("FAILED", "FAILED", "CAPTCHA_UNSOLVED"),
    "REGISTER_ERROR": ("FAILED", "FAILED", "REGISTER_ERROR"),
}


class AsinPageGuard:
    def __init__(self, app: SessionFactory, cfg: GuardConfig = GuardConfig()) -> None:
        self._app, self._cfg = app, cfg

    @staticmethod
    def _rows(conn, market: str, target: str) -> tuple[list[InvestigationRow], datetime]:
        rows = [
            InvestigationRow(r.status, r.outcome, r.started_at, r.completed_at)
            for r in conn.execute(
                text("SELECT status, outcome, started_at, completed_at FROM collector_run WHERE run_type = :rt "
                     "AND marketplace = :m AND target = :t AND source_id IS NULL ORDER BY id"),
                {"rt": RUN_TYPE, "m": market, "t": target},
            )
        ]
        return rows, conn.execute(text("SELECT clock_timestamp()")).scalar_one()

    def state(self, market: str, asin: str, routes: Iterable[str]) -> tuple[str, datetime | None]:
        """読み取りだけ（記録しない）。"""
        with self._app.get_session(market) as conn, conn.begin():
            rows, now = self._rows(conn, market, investigation_target(asin, routes))
        return decide_investigation(rows, now, self._cfg)

    def begin(self, market: str, asin: str, routes: Iterable[str]) -> int:
        """調べてよければ実行中の行を作ってrun_idを返す。だめなら InvestigationBlocked。"""
        target = investigation_target(asin, routes)
        with self._app.get_session(market) as conn, conn.begin():
            identity_lock(conn, market, RUN_TYPE, target)
            rows, now = self._rows(conn, market, target)
            state, until = decide_investigation(rows, now, self._cfg)
            if state != "ELIGIBLE":
                raise InvestigationBlocked(state, until)
            conn.execute(  # 放置された実行中の行は中断として確定する（再探索を永久にふさがない）
                text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = 'LEASE_EXPIRED', "
                     "completed_at = clock_timestamp(), error_message = 'stale ASIN_PAGE run' WHERE run_type = :rt AND marketplace = :m "
                     "AND target = :t AND source_id IS NULL AND status IN ('RUNNING','CAPTCHA_WAIT')"),
                {"rt": RUN_TYPE, "m": market, "t": target},
            )
            return conn.execute(
                text("INSERT INTO collector_run (marketplace, run_type, target, status) VALUES (:m, :rt, :t, 'RUNNING') RETURNING id"),
                {"m": market, "rt": RUN_TYPE, "t": target},
            ).scalar_one()

    def finish(self, market: str, run_id: int, kind: str, *, candidates: int = 0, detail: str | None = None) -> None:
        status, outcome, code = _FINISH[kind]
        with self._app.get_session(market) as conn, conn.begin():
            conn.execute(
                text("UPDATE collector_run SET status = :s, outcome = :o, error_code = :c, error_message = :d, asin_count = :n, "
                     "completed_at = clock_timestamp() WHERE id = :id AND run_type = :rt AND source_id IS NULL"),
                {"s": status, "o": outcome, "c": code, "d": detail, "n": candidates, "id": run_id, "rt": RUN_TYPE},
            )

    def interrupt(self, market: str, run_id: int) -> None:
        with self._app.get_session(market) as conn, conn.begin():
            conn.execute(
                text("UPDATE collector_run SET status = 'INTERRUPTED', outcome = 'INTERRUPTED', interrupt_kind = 'USER', "
                     "completed_at = clock_timestamp() WHERE id = :id AND run_type = :rt AND source_id IS NULL AND status = 'RUNNING'"),
                {"id": run_id, "rt": RUN_TYPE},
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
            f"市場{market}の自社セラーIDが未設定です（marketplace_account_config に is_active=true で登録してください）。"
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


def build_candidates(result, routes: Iterable[str], page_market: str, own_ids: set[str], limit: int,
                     host: str) -> tuple[list[SourceCandidate], list[Skipped]]:
    """商品ページの解析結果から、登録する候補を上限つきで選ぶ（ブランド→セラーの順。自社・Amazonは除く）。"""
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
            if s.seller_id in own_ids:
                skipped.append(Skipped(s.seller_id, "OWN_SELLER"))
            elif s.seller_id in AMAZON_RETAIL_SELLER_IDS.get(page_market, set()):
                skipped.append(Skipped(s.seller_id, "AMAZON_RETAIL"))
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
class OriginSelection:
    selected: list[Candidate]
    skipped: list[tuple[str, str, datetime | None]] = field(default_factory=list)  # (asin, state, until)
    origin_market: str | None = None


class DiscoveryService:
    def __init__(self, app: SessionFactory, guard: AsinPageGuard | None = None, actor: str | None = None) -> None:
        self._app = app
        self.guard = guard or AsinPageGuard(app)
        self._registry = SourceRegistry(app, actor=actor)
        self._reports = ReportImportService(app, None, actor=actor)

    # ---- 起点の選択
    def select_origins(self, origin_import_id: int, page_market: str, routes: Iterable[str], limits: DiscoveryLimits,
                       include_zero_units: bool = False) -> OriginSelection:
        limits.validate()
        info = validate_origin_imports(self._reports, [origin_import_id])[0]
        with self._app.get_session(info.marketplace) as conn, conn.begin():
            ranked = fetch_candidates(conn, origin_import_id)
        sel = OriginSelection([], origin_market=info.marketplace)
        for c in ranked:
            if c.units_sum <= 0 and not include_zero_units:
                continue  # 注文0点の候補は(経路としては残るが)既定では調べない
            state, until = self.guard.state(page_market, c.asin, routes)
            if state != "ELIGIBLE":
                sel.skipped.append((c.asin, state, until))
                continue
            sel.selected.append(c)
            if len(sel.selected) >= limits.max_origin_asins:
                break
        return sel

    # ---- 起点ASINの発見結果の登録
    def record_origin(self, origin: Candidate, page_market: str, page_url: str, result, routes: Iterable[str],
                      own_ids: set[str], limits: DiscoveryLimits, run_id: int) -> tuple[list[Registered], list[Skipped]]:
        host = page_url.split("/")[2]
        cands, skipped = build_candidates(result, routes, page_market, own_ids, limits.max_sources_per_origin, host)
        out: list[Registered] = []
        for c in cands:
            note = json.dumps({"v": 1, "route": "ORIGIN_PRODUCT_PAGE", "extracted_from": "PRODUCT_PAGE", "page_market": page_market,
                               "page_asin": origin.asin, "page_url": page_url, "investigation_run": run_id, **c.meta},
                              ensure_ascii=False)[:900]
            rp = self._registry.register_path(page_market, c.source_type, c.key, "", origin_import_id=origin.import_id,
                                              origin_asin=origin.asin, note=note)
            out.append(Registered(rp.source.source_id, c.source_type, c.key, rp.source.created, rp.path_created))
        return out, skipped

    # ---- 反対側市場への展開
    def select_cross_asins(self, via_market: str, via_source_id: int, page_market: str, routes: Iterable[str],
                           limits: DiscoveryLimits) -> tuple[list[str], list[tuple[str, str, datetime | None]], int]:
        """取得済みの出典(via_market, via_source_id)の最新の完了した実行のASINのうち、反対側市場page_marketで調べてよいもの。"""
        limits.validate()
        if via_market == page_market:
            raise DiscoveryError("反対側の市場を指定してください（元の出典の市場と商品ページの市場が同じです）")
        with self._app.get_session(via_market) as conn, conn.begin():
            src = conn.execute(text("SELECT source_id FROM acquisition_source WHERE source_id = :s"), {"s": via_source_id}).one_or_none()
            if src is None:
                raise DiscoveryError(f"source_id={via_source_id} は市場{via_market}に存在しません")
            run = conn.execute(
                text("SELECT id FROM collector_run WHERE source_id = :s AND status = 'COMPLETED' ORDER BY completed_at DESC, id DESC LIMIT 1"),
                {"s": via_source_id},
            ).one_or_none()
            if run is None:
                raise DiscoveryError(f"source_id={via_source_id} には完了した取得がありません（取得済みの出典だけから展開します）")
            asins = [r[0] for r in conn.execute(
                text("SELECT asin FROM collector_asin WHERE collector_run_id = :r ORDER BY id"), {"r": run.id})]
        chosen: list[str] = []
        skipped: list[tuple[str, str, datetime | None]] = []
        for a in asins:
            state, until = self.guard.state(page_market, a, routes)
            if state != "ELIGIBLE":
                skipped.append((a, state, until))
                continue
            chosen.append(a)
            if len(chosen) >= limits.max_cross_asins:
                break
        return chosen, skipped, run.id

    def record_cross(self, via_market: str, via_source_id: int, via_asin: str, page_market: str, page_url: str, result,
                     routes: Iterable[str], own_ids: set[str], limits: DiscoveryLimits, run_id: int
                     ) -> tuple[list[Registered], list[Skipped]]:
        host = page_url.split("/")[2]
        cands, skipped = build_candidates(result, routes, page_market, own_ids, limits.max_cross_sources_per_asin, host)
        out: list[Registered] = []
        for c in cands:
            note = json.dumps({"v": 1, "route": "CROSS_MARKET_PRODUCT_PAGE", "extracted_from": "PRODUCT_PAGE", "page_market": page_market,
                               "page_asin": via_asin, "page_url": page_url, "via_market": via_market, "investigation_run": run_id,
                               "asin_existence_in_page_market": "FOUND", **c.meta}, ensure_ascii=False)[:900]
            rp = self._registry.register_via_path(page_market, c.source_type, c.key, "", via_market=via_market,
                                                  via_source_id=via_source_id, via_asin=via_asin, note=note)
            out.append(Registered(rp.source.source_id, c.source_type, c.key, rp.source.created, rp.path_created))
        return out, skipped
