"""出典の登録・発見経路の登録・保留/除外/解除/間隔変更。

- 出典は(取得先市場, 種別, 構造化キー)で一意。同じ出典を別の起点・輸出入の経路から発見しても
  出典は1つで、発見経路(acquisition_path)だけが追加される（取得は共有される）。
- 経路の登録時に、起点の指標（注文点数・セッション数・最大獲得率）を**その時点の指定レポートから
  コピー**して保存する。順位付けは取得先市場の区画内で完結し、区画をまたぐ読み取りは登録時の
  短い読み取り(起点レポートの区画)だけ。レポートはimport_idで明示し、ACTIVEで行エラー0件の
  ものだけを受け付ける。
- 判定を変える全ての操作（保留・除外・解除・間隔変更）は、claimと同じ識別ロックと出典行ロックに
  参加し、履歴(acquisition_source_event)を残す。
"""
from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

from acquisition.locks import identity_lock
from acquisition.normalize import KeyCollisionError, key_hash, normalize_key
from db.session import SessionFactory
from origin_selection.report_import import ReportImportService

DIRECTION_BY_MARKET = {"US": "EXPORT", "JP": "IMPORT"}


class OriginError(Exception):
    """起点レポート・ASINを経路の根拠として使えない。"""


@dataclass(frozen=True)
class RegisteredSource:
    source_id: int
    created: bool
    marketplace: str
    source_type: str
    key_primary: str
    key_qualifier: str


@dataclass(frozen=True)
class RegisteredPath:
    source: RegisteredSource
    path_created: bool


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class SourceRegistry:
    def __init__(self, app_factory: SessionFactory, actor: str | None = None) -> None:
        self._app = app_factory
        self._actor = actor or _actor()
        self._reports = ReportImportService(app_factory, None, actor=self._actor)

    # ------------------------------------------------------------------ 出典

    def register_source(self, marketplace: str, source_type: str, primary: str, qualifier: str = "") -> RegisteredSource:
        key_primary, key_qualifier = normalize_key(source_type, primary, qualifier)
        h = key_hash(source_type, key_primary, key_qualifier)
        with self._app.get_session(marketplace) as conn, conn.begin():
            identity_lock(conn, marketplace, source_type, h)
            row = conn.execute(
                text(
                    "SELECT source_id, key_primary, key_qualifier FROM acquisition_source "
                    "WHERE marketplace = :mp AND source_type = :t AND key_hash = :h"
                ),
                {"mp": marketplace, "t": source_type, "h": h},
            ).one_or_none()
            if row is not None:
                if row.key_primary != key_primary or row.key_qualifier != key_qualifier:
                    raise KeyCollisionError(f"key_hash collision for {source_type}:{key_primary!r}")
                return RegisteredSource(row.source_id, False, marketplace, source_type, key_primary, key_qualifier)
            source_id = conn.execute(
                text(
                    "INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_qualifier, key_hash, "
                    "display_primary, display_qualifier) VALUES (:mp, :t, :kp, :kq, :h, :dp, :dq) RETURNING source_id"
                ),
                {"mp": marketplace, "t": source_type, "kp": key_primary, "kq": key_qualifier, "h": h,
                 "dp": primary.strip(), "dq": (qualifier or "").strip()},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) "
                    "VALUES (:id, :mp, 'REGISTERED', :d, :a)"
                ),
                {"id": source_id, "mp": marketplace, "d": f"{source_type}:{primary.strip()}", "a": self._actor},
            )
        return RegisteredSource(source_id, True, marketplace, source_type, key_primary, key_qualifier)

    # ------------------------------------------------------------------ 発見経路

    def _origin_metrics(self, origin_import_id: int, origin_asin: str) -> tuple[str, int, int, object]:
        origin_mp = self._reports.resolve_market_for_import(origin_import_id)
        if origin_mp is None:
            raise OriginError(f"import_id={origin_import_id} は存在しません")
        info = self._reports.get_report(origin_import_id, origin_mp)
        if info is None or info.status != "ACTIVE":
            raise OriginError(f"import_id={origin_import_id} は有効版（ACTIVE）ではありません")
        if info.error_rows != 0:
            raise OriginError(f"import_id={origin_import_id} は行エラーのあるレポートです")
        asin = origin_asin.strip().upper()
        with self._app.get_session(origin_mp) as conn, conn.begin():
            row = conn.execute(
                text(
                    "SELECT units_sum, sessions_sum, buybox_rate_max_pct FROM v_business_report_candidate "
                    "WHERE import_id = :i AND asin = :a"
                ),
                {"i": origin_import_id, "a": asin},
            ).one_or_none()
        if row is None:
            raise OriginError(f"ASIN {asin} は import_id={origin_import_id} に存在しません")
        return origin_mp, int(row.units_sum), int(row.sessions_sum), row.buybox_rate_max_pct

    def register_path(
        self,
        marketplace: str,
        source_type: str,
        primary: str,
        qualifier: str = "",
        *,
        origin_import_id: int | None = None,
        origin_asin: str | None = None,
        note: str | None = None,
    ) -> RegisteredPath:
        """取得先市場`marketplace`の出典を登録（無ければ作成）し、発見経路を追加する。
        起点を指定した場合、起点レポートの区画で指標を読み、経路にコピーする（別セッション）。"""
        if (origin_import_id is None) != (origin_asin is None):
            raise OriginError("起点のimport_idとASINは両方指定してください")
        origin = self._origin_metrics(origin_import_id, origin_asin) if origin_import_id is not None else None
        source = self.register_source(marketplace, source_type, primary, qualifier)
        with self._app.get_session(marketplace) as conn, conn.begin():
            if origin is None:
                params = {"stage": "MANUAL", "direction": None, "om": None, "oi": None, "oa": None,
                          "ou": None, "os": None, "ob": None}
            else:
                om, units, sessions, buybox = origin
                params = {"stage": "ORIGIN_ASIN", "direction": DIRECTION_BY_MARKET[om], "om": om,
                          "oi": origin_import_id, "oa": origin_asin.strip().upper(), "ou": units,
                          "os": sessions, "ob": buybox}
            created = conn.execute(
                text(
                    "INSERT INTO acquisition_path (source_id, marketplace, direction, stage, origin_marketplace, "
                    "origin_import_id, origin_asin, origin_units, origin_sessions, origin_buybox_max_pct, note, "
                    "registered_by) VALUES (:sid, :mp, :direction, :stage, :om, :oi, :oa, :ou, :os, :ob, :note, :by) "
                    "ON CONFLICT DO NOTHING RETURNING path_id"
                ),
                {"sid": source.source_id, "mp": marketplace, "note": note, "by": self._actor, **params},
            ).one_or_none()
        return RegisteredPath(source, created is not None)

    def register_via_path(
        self,
        marketplace: str,
        source_type: str,
        primary: str,
        qualifier: str = "",
        *,
        via_market: str,
        via_source_id: int,
        via_asin: str,
        note: str | None = None,
        allowed_origin_import_ids: list[int] | None = None,
    ) -> RegisteredPath:
        """取得済みの出典(via_market, via_source_id)が持っていたASINから、別の市場`marketplace`で見つけた出典を登録する
        （VIA_SOURCE経路）。起点の指標・方向は、元の出典の代表起点（注文点数→セッション→獲得率が最良の起点）から
        **コピー**し、順位を引き継ぐ。`allowed_origin_import_ids`を指定したときは、そのimportに属する起点経路だけを代表起点にし、
        無ければ登録しない（OriginError）。未指定で元の出典に起点が無ければ、起点なし（方向・指標なし）の経路になる。"""
        with self._app.get_session(via_market) as conn, conn.begin():
            rep = conn.execute(
                text(
                    "SELECT direction, origin_marketplace, origin_import_id, origin_asin, origin_units, origin_sessions, "
                    "origin_buybox_max_pct FROM acquisition_path WHERE source_id = :sid AND marketplace = :mp "
                    "AND stage = 'ORIGIN_ASIN' AND (CAST(:ids AS bigint[]) IS NULL OR origin_import_id = ANY(:ids)) "
                    "ORDER BY origin_units DESC, origin_sessions DESC, origin_buybox_max_pct DESC, origin_asin LIMIT 1"
                ),
                {"sid": via_source_id, "mp": via_market, "ids": allowed_origin_import_ids},
            ).one_or_none()
        if allowed_origin_import_ids is not None and rep is None:
            # 指定したimport以外（過去レポート）の経路は代表起点に使わない。起点なしの経路にもしない（登録しない）。
            raise OriginError(f"source_id={via_source_id} には指定したimport {allowed_origin_import_ids} の起点経路がありません")
        source = self.register_source(marketplace, source_type, primary, qualifier)
        params = {"direction": None, "om": None, "oi": None, "oa": None, "ou": None, "os": None, "ob": None}
        if rep is not None:
            params = {"direction": rep.direction, "om": rep.origin_marketplace, "oi": rep.origin_import_id,
                      "oa": rep.origin_asin, "ou": rep.origin_units, "os": rep.origin_sessions, "ob": rep.origin_buybox_max_pct}
        with self._app.get_session(marketplace) as conn, conn.begin():
            created = conn.execute(
                text(
                    "INSERT INTO acquisition_path (source_id, marketplace, direction, stage, origin_marketplace, "
                    "origin_import_id, origin_asin, origin_units, origin_sessions, origin_buybox_max_pct, via_source_id, "
                    "via_asin, note, registered_by) VALUES (:sid, :mp, :direction, 'VIA_SOURCE', :om, :oi, :oa, :ou, :os, "
                    ":ob, :vs, :va, :note, :by) ON CONFLICT DO NOTHING RETURNING path_id"
                ),
                {"sid": source.source_id, "mp": marketplace, "vs": via_source_id, "va": via_asin.strip().upper(),
                 "note": note, "by": self._actor, **params},
            ).one_or_none()
        return RegisteredPath(source, created is not None)

    # ------------------------------------------------------------------ 状態・間隔の変更

    def _locked_source(self, conn: Connection, marketplace: str, source_id: int):
        ident = conn.execute(
            text("SELECT source_type, key_hash FROM acquisition_source WHERE source_id = :id"), {"id": source_id}
        ).one_or_none()
        if ident is None:
            raise ValueError(f"source_id={source_id} は市場{marketplace}に存在しません")
        identity_lock(conn, marketplace, ident.source_type, ident.key_hash.strip())
        return conn.execute(
            text("SELECT source_id, status, hold_reason FROM acquisition_source WHERE source_id = :id FOR UPDATE"),
            {"id": source_id},
        ).one()

    def _event(self, conn: Connection, source_id: int, marketplace: str, event_type: str, detail: str) -> None:
        conn.execute(
            text(
                "INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor) "
                "VALUES (:id, :mp, :t, :d, :a)"
            ),
            {"id": source_id, "mp": marketplace, "t": event_type, "d": detail, "a": self._actor},
        )

    def set_status(
        self, marketplace: str, source_id: int, status: Literal["ON_HOLD", "EXCLUDED"], reason: str
    ) -> None:
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        with self._app.get_session(marketplace) as conn, conn.begin():
            self._locked_source(conn, marketplace, source_id)
            conn.execute(
                text(
                    "UPDATE acquisition_source SET status = :s, hold_reason = 'MANUAL', hold_detail = :d, "
                    "held_at = clock_timestamp() WHERE source_id = :id"
                ),
                {"s": status, "d": reason.strip(), "id": source_id},
            )
            self._event(conn, source_id, marketplace, "HOLD" if status == "ON_HOLD" else "EXCLUDE", reason.strip())

    def release(self, marketplace: str, source_id: int, reason: str) -> None:
        """保留・除外・自動保留を明示的に解除する。以降、それ以前の失敗は連続失敗に数えない。
        保留されていない（ACTIVE）出典でも、一巡の取り直しの上限（CYCLE_RESTART_LIMIT）で止まっている場合は解除できる
        （上限を設定で下げた・0018より前の記録などで、自動保留を経ずに上限に達した出典。表示の案内どおり release で解除できるようにする）。
        解除の記録（failure_streak_reset_at）が一巡の終了より後になるため、同じ履歴で再び上限に止められない。"""
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        with self._app.get_session(marketplace) as conn, conn.begin():
            row = self._locked_source(conn, marketplace, source_id)
            if row.status == "ACTIVE":
                from acquisition.eligibility import db_now, decide, load_configs, load_facts

                facts = load_facts(conn, marketplace, [source_id])[0]
                d = decide(facts, load_configs(conn, marketplace)[facts.source_type], db_now(conn))
                if not any(b.code == "CYCLE_RESTART_LIMIT" for b in d.blockers):
                    raise ValueError("保留・除外されていません")
                conn.execute(
                    text("UPDATE acquisition_source SET failure_streak_reset_at = clock_timestamp() WHERE source_id = :id"),
                    {"id": source_id},
                )
                self._event(conn, source_id, marketplace, "RELEASE", f"ACTIVE/CYCLE_RESTART_LIMIT → 取り直しの上限を解除: {reason.strip()}")
                return
            conn.execute(
                text(
                    "UPDATE acquisition_source SET status = 'ACTIVE', hold_reason = NULL, hold_detail = NULL, "
                    "held_at = NULL, failure_streak_reset_at = clock_timestamp() WHERE source_id = :id"
                ),
                {"id": source_id},
            )
            self._event(conn, source_id, marketplace, "RELEASE", f"{row.status}/{row.hold_reason} → ACTIVE: {reason.strip()}")

    def set_interval(self, marketplace: str, source_id: int, days: int | None, reason: str) -> None:
        if days is not None and days <= 0:
            raise ValueError("daysは1以上")
        if not reason or not reason.strip():
            raise ValueError("理由が必須です")
        with self._app.get_session(marketplace) as conn, conn.begin():
            self._locked_source(conn, marketplace, source_id)
            conn.execute(
                text("UPDATE acquisition_source SET interval_override_days = :d WHERE source_id = :id"),
                {"d": days, "id": source_id},
            )
            self._event(conn, source_id, marketplace, "INTERVAL_SET", f"{days if days else '既定'}日: {reason.strip()}")
