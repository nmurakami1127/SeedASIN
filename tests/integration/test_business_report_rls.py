"""ビジネスレポート3表・2ビューのRLS／権限／制約の結合テスト（3ロール、実PostgreSQL、使い捨てtest-db）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from db.session import SessionFactory
from origin_selection.report_import import ReportImportService
from tests.integration.conftest import role_url
from tests.integration.report_helpers import fixture_bytes, place, unique_period

pytestmark = pytest.mark.integration

_TABLES_AND_VIEWS = (
    "business_report_import",
    "business_report_row",
    "business_report_import_event",
    "v_business_report_asin",
    "v_business_report_candidate",
)


@pytest.fixture()
def service(app_role_factory, kpi_report_factory) -> ReportImportService:
    return ReportImportService(app_role_factory, kpi_report_factory, actor="tester")


@pytest.fixture()
def both(tmp_path, service):
    us, _ = place(tmp_path, "US", "u.csv", fixture_bytes("synthetic_us.csv"))
    jp, _ = place(tmp_path, "JP", "j.csv", fixture_bytes("synthetic_jp.csv"))
    ps, pe = unique_period()
    return service.import_file(us, ps, pe).report, service.import_file(jp, ps, pe).report


@pytest.mark.parametrize("relation", _TABLES_AND_VIEWS)
def test_unset_marketplace_is_denied(superuser_url, relation):
    engine = create_engine(role_url(superuser_url, "app_role"))
    try:
        with engine.connect() as conn:
            with pytest.raises(ProgrammingError, match="unrecognized configuration parameter"):
                conn.execute(text(f"SELECT count(*) FROM {relation}")).scalar_one()
    finally:
        engine.dispose()


def test_us_session_cannot_see_jp_rows_in_tables_or_views(app_role_factory, both):
    us, jp = both
    with app_role_factory.get_session("US") as conn, conn.begin():
        for rel, col in (
            ("business_report_import", "import_id"),
            ("business_report_row", "import_id"),
            ("business_report_import_event", "import_id"),
            ("v_business_report_asin", "import_id"),
            ("v_business_report_candidate", "import_id"),
        ):
            ids = {r[0] for r in conn.execute(text(f"SELECT DISTINCT {col} FROM {rel}")).all()}
            assert us.import_id in ids and jp.import_id not in ids, rel
    with app_role_factory.get_session("JP") as conn, conn.begin():
        ids = {r[0] for r in conn.execute(text("SELECT DISTINCT import_id FROM v_business_report_candidate")).all()}
        assert jp.import_id in ids and us.import_id not in ids


def test_us_session_cannot_insert_jp_row(app_role_factory, both):
    us, jp = both
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="row-level security"):
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO business_report_import_event (import_id, marketplace, event_type, actor) "
                        "VALUES (:i, 'JP', 'IMPORTED', 'x')"
                    ),
                    {"i": jp.import_id},
                )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE business_report_row SET sessions = 0",
        "DELETE FROM business_report_row",
        "UPDATE business_report_import_event SET note = 'x'",
        "DELETE FROM business_report_import_event",
        "DELETE FROM business_report_import",
        "UPDATE business_report_import SET period_start = period_start",  # 不変列
        "UPDATE business_report_import SET file_sha256 = file_sha256",
        "UPDATE business_report_import SET error_rows = 0",
    ],
)
def test_app_role_cannot_modify_append_only_or_immutable_columns(app_role_factory, both, sql):
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(text(sql))


def test_app_role_can_update_status_columns_only(app_role_factory, both):
    us, _ = both
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text("UPDATE business_report_import SET status='REJECTED' WHERE import_id=:i"), {"i": us.import_id}
        )
        conn.execute(
            text("UPDATE business_report_import SET status='STAGED' WHERE import_id=:i"), {"i": us.import_id}
        )


def test_kpi_report_role_is_read_only_and_sees_both_markets(kpi_report_factory, both):
    us, jp = both
    with kpi_report_factory.get_session() as conn:
        ids = {r[0] for r in conn.execute(text("SELECT import_id FROM business_report_import")).all()}
        assert {us.import_id, jp.import_id} <= ids
        views = {r[0] for r in conn.execute(text("SELECT DISTINCT import_id FROM v_business_report_candidate")).all()}
        assert {us.import_id, jp.import_id} <= views
    with kpi_report_factory.get_session() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(text("UPDATE business_report_import SET status = status"))
    with kpi_report_factory.get_session() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(
                    text("INSERT INTO business_report_import_event (import_id, marketplace, event_type, actor) "
                         "VALUES (1, 'US', 'IMPORTED', 'x')")
                )


def test_admin_role_has_no_access(admin_role_factory, both):
    with admin_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(text("SELECT count(*) FROM business_report_import"))


# ---- 制約（RLSを持たない所有者接続で、CHECK/FKそのものを検証する）----


@pytest.fixture()
def owner(superuser_url):
    engine = create_engine(superuser_url)
    yield engine
    engine.dispose()


def _insert_import_sql(**over) -> tuple[str, dict]:
    params = dict(
        mp="US", direction="EXPORT", basis="FOLDER", ps="2999-01-01", pe="2999-01-31", pbasis="USER_SPECIFIED",
        sha="f" * 64, run="US-BUSINESS_REPORT-x-0000", total=0, loaded=0, errors=0,
    )
    params.update(over)
    sql = (
        "INSERT INTO business_report_import (marketplace, business_direction, market_basis, period_start, "
        "period_end, period_basis, source_file_name, source_path, file_sha256, encoding_detected, header_text, "
        "import_run_id, total_rows, loaded_rows, error_rows) VALUES (:mp, :direction, :basis, :ps, :pe, :pbasis, "
        "'f.csv', 'f.csv', :sha, 'utf-8', 'h', :run, :total, :loaded, :errors)"
    )
    return sql, params


@pytest.mark.parametrize(
    "over, constraint",
    [
        ({"direction": "IMPORT"}, "ck_bri_direction"),  # USは輸出のみ
        ({"mp": "JP", "direction": "EXPORT"}, "ck_bri_direction"),
        ({"basis": "FILENAME"}, "ck_bri_market_basis"),
        ({"pbasis": "FILE_VERIFIED"}, "ck_bri_period_basis"),
        ({"ps": "2999-02-01", "pe": "2999-01-01"}, "ck_bri_period"),
        ({"total": 5, "loaded": 1, "errors": 1}, "ck_bri_counts"),
    ],
)
def test_import_check_constraints(owner, both, over, constraint):
    sql, params = _insert_import_sql(**over)
    # FK先のimport_run_historyが必要
    with owner.connect() as conn:
        with pytest.raises(IntegrityError, match=constraint):
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO import_run_history (import_run_id, marketplace, import_type, source_file_name, executed_at) "
                        "VALUES (:run, 'US', 'BUSINESS_REPORT', 'f.csv', now()) ON CONFLICT DO NOTHING"
                    ),
                    {"run": params["run"]},
                )
                conn.execute(text(sql), params)


def test_row_with_mismatched_marketplace_violates_composite_fk(owner, both):
    us, _ = both
    with owner.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_brr_import"):
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO business_report_row (import_id, marketplace, row_number, child_asin, sessions, "
                        "buybox_rate_pct, units_ordered, raw_line) VALUES (:i, 'JP', 9999, 'B0TESTXX01', 1, 1, 1, 'x')"
                    ),
                    {"i": us.import_id},
                )


@pytest.mark.parametrize(
    "asin, units, buybox",
    [("B0SHORT", 1, 1), ("b0lowercase", 1, 1), ("B0TESTXX01", -1, 1), ("B0TESTXX01", 1, 100.01)],
)
def test_row_value_checks(owner, both, asin, units, buybox):
    us, _ = both
    with owner.connect() as conn:
        with pytest.raises(IntegrityError, match="ck_brr_"):
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO business_report_row (import_id, marketplace, row_number, child_asin, sessions, "
                        "buybox_rate_pct, units_ordered, raw_line) VALUES (:i, 'US', 9998, :a, 1, :b, :u, 'x')"
                    ),
                    {"i": us.import_id, "a": asin, "b": buybox, "u": units},
                )
