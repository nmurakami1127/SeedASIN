"""acquisition系の結合テスト共通ヘルパー。"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from acquisition.lease import AcquisitionRunService
from acquisition.registry import SourceRegistry


def unique_key(prefix: str = "A") -> str:
    return prefix + uuid.uuid4().hex[:12].upper()


def new_source(registry: SourceRegistry, market: str = "US", source_type: str = "SELLER", key: str | None = None,
               qualifier: str = ""):
    return registry.register_source(market, source_type, key or unique_key(), qualifier)


def expire_run(factory, market: str, run_id: int, seconds: int = 5) -> None:
    """lease期限を過去へ動かす（テスト用: 実時間を待たずに期限切れを再現する）。"""
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(
            text("UPDATE collector_run SET lease_expires_at = clock_timestamp() - make_interval(secs => :s) WHERE id = :id"),
            {"s": seconds, "id": run_id},
        )


def run_row(factory, market: str, run_id: int):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(
            text("SELECT status, outcome, interrupt_kind, completeness, pages_fetched, asin_count, "
                 "(lease_expires_at > clock_timestamp()) AS lease_ok, lease_seconds FROM collector_run WHERE id = :id"),
            {"id": run_id},
        ).one()


def asin_count(factory, market: str, run_id: int) -> int:
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :id"), {"id": run_id}).scalar_one()


def set_config(factory, market: str, source_type: str, **cols) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(
            text(f"UPDATE acquisition_interval_config SET {sets} WHERE marketplace = :mp AND source_type = :t"),
            {**cols, "mp": market, "t": source_type},
        )


def make_runs(factory) -> AcquisitionRunService:
    return AcquisitionRunService(factory, actor="tester")


_US_HEADER = (
    "（子）ASIN,セッション数 - 合計,セッション率 - 合計,おすすめ出品（おすすめ商品）の獲得率,"
    "注文された商品点数,ユニットセッション率,注文商品の売上額,注文品目総数"
)


def make_active_report(app_factory, tmp_path, market: str, rows: list[tuple[str, int, float, int]], period=None,
                       name: str = "r.csv"):
    """(asin, sessions, buybox%, units) の行から、ACTIVEなビジネスレポートを作る。import_idを返す。"""
    from origin_selection.report_import import ReportImportService
    from tests.integration.report_helpers import place, unique_period

    body = "\n".join([_US_HEADER] + [f"{a},{s},0%,{b}%,{u},0%,$0,{u}" for a, s, b, u in rows])
    res, _ = place(tmp_path, market, name, body.encode("utf-8"))
    svc = ReportImportService(app_factory, None, actor="tester")
    info = svc.import_file(res, *(period or unique_period())).report
    preview = svc.preview_activation(info.import_id, market, "first")
    svc.activate(info.import_id, market, "first", preview, "test")
    return info.import_id


def insert_old_success(factory, market: str, source_id: int, days_ago: float, outcome: str = "SUCCESS_NONEMPTY") -> int:
    """過去の成功実行を直接INSERTする（終了済みの行はトリガーで更新できないため、時刻を動かす代わりに
    最初から過去の時刻で作る。INSERTは保護の対象外）。"""
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status, started_at, completed_at, source_id, "
                "attempt_token, heartbeat_at, lease_expires_at, lease_seconds, outcome, completeness) "
                "VALUES (:mp, 'SELLER_STOREFRONT', 'old', 'COMPLETED', clock_timestamp() - make_interval(days => :d) - interval '1 hour', "
                "clock_timestamp() - make_interval(days => :d), :sid, gen_random_uuid(), clock_timestamp(), clock_timestamp(), 300, "
                ":o, 'COMPLETE') RETURNING id"
            ),
            {"mp": market, "d": days_ago, "sid": source_id, "o": outcome},
        ).scalar_one()
