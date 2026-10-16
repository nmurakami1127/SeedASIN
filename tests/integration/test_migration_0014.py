"""alembic 0014 の検証（実PostgreSQL、使い捨てtest-db）。

- 空DB: conftestの_migrated_schemaがモジュールごとにalembic upgrade headを空DBから実行する（head=0016）
  （0001〜0013→0014→0015）。
- 既存DB: 0013まで戻して既存テーブルにデータを入れ、0014・0015だけを適用して無傷を確認する。
- 0015（DEACTIVATEDイベント種別・理由必須CHECK）: 0014適用済みDBへの追加適用、再実行、downgradeを確認する。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from db.table_names import ALL_TABLES, INCREMENTAL_RLS_TABLES

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "alembic" / "versions" / "0014_create_business_report_tables.py"
_MIGRATION_0015 = _ROOT / "alembic" / "versions" / "0015_add_deactivated_event_type.py"


def _alembic(superuser_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["SEEDASIN_DATABASE_URL"] = superuser_url
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, cwd=_ROOT, env=env)


def _tables(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'business_report%'")
        )
    }


def test_fresh_build_creates_tables_rls_policies_grants_and_views(superuser_url):
    engine = create_engine(superuser_url)
    with engine.connect() as conn:
        assert _tables(conn) == set(INCREMENTAL_RLS_TABLES)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016"
        rls = {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT relname, relrowsecurity FROM pg_class WHERE relname LIKE 'business_report%' AND relkind='r'")
            )
        }
        assert rls == {t: True for t in INCREMENTAL_RLS_TABLES}
        policies = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_policies WHERE tablename LIKE 'business_report%'"))}
        assert policies == set(INCREMENTAL_RLS_TABLES)
        views = {r[0]: r[1] for r in conn.execute(text("SELECT c.relname, c.reloptions::text FROM pg_class c WHERE c.relkind='v' AND c.relname LIKE 'v_business_report%'"))}
        assert set(views) == {"v_business_report_asin", "v_business_report_candidate"}
        assert all("security_invoker=true" in v for v in views.values())
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname='uk_bri_one_active_per_period'")).scalar_one() == 1
    engine.dispose()


def test_incremental_tables_are_not_in_all_tables():
    """0003/0004が実行時に読むALL_TABLESに含めない（含めると空DBの初回構築で失敗する）。"""
    assert not set(INCREMENTAL_RLS_TABLES) & set(ALL_TABLES)


def test_import_type_check_accepts_business_report(superuser_url):
    engine = create_engine(superuser_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO import_run_history (import_run_id, marketplace, import_type, source_file_name, executed_at) "
                "VALUES ('US-BUSINESS_REPORT-mig-0001', 'US', 'BUSINESS_REPORT', 'm.csv', now())"
            )
        )
        conn.execute(text("DELETE FROM import_run_history WHERE import_run_id='US-BUSINESS_REPORT-mig-0001'"))
    with pytest.raises(IntegrityError, match="ck_import_type"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO import_run_history (import_run_id, marketplace, import_type, source_file_name, executed_at) "
                    "VALUES ('US-BOGUS-mig-0001', 'US', 'BOGUS', 'm.csv', now())"
                )
            )
    engine.dispose()


def test_upgrade_function_is_idempotent_when_rerun(superuser_url):
    spec = importlib.util.spec_from_file_location("mig0014", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(superuser_url)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()  # 既に適用済みの状態で再実行しても失敗しない
            module.upgrade()
        assert _tables(conn) == set(INCREMENTAL_RLS_TABLES)
    engine.dispose()


def test_existing_db_path_preserves_data_and_downgrade_roundtrip(superuser_url, app_role_factory, kpi_report_factory, tmp_path):
    """既存DB(0013適用済み)へ0014だけを適用する経路。既存データが無傷であること、
    downgradeで0013の状態に戻り、再upgradeできること。"""
    from origin_selection.report_import import ReportImportService
    from tests.integration.report_helpers import fixture_bytes, place, unique_period

    # 0014のデータ（downgrade時に削除されることを確認する）
    res, _ = place(tmp_path, "US", "m.csv", fixture_bytes("synthetic_us.csv"))
    ReportImportService(app_role_factory, kpi_report_factory).import_file(res, *unique_period())

    engine = create_engine(superuser_url)
    _alembic(superuser_url, "downgrade", "0013")
    with engine.begin() as conn:
        assert _tables(conn) == set()
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013"
        # 旧CHECKに戻っている
        with pytest.raises(IntegrityError, match="ck_import_type"):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO import_run_history (import_run_id, marketplace, import_type, source_file_name, executed_at) "
                        "VALUES ('US-BUSINESS_REPORT-old-0001', 'US', 'BUSINESS_REPORT', 'm.csv', now())"
                    )
                )
        # 既存テーブルに実データ相当の行を入れる（所有者接続はRLSを受けない）
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, known_at_reliable, last_seen_at) "
                "VALUES ('US', 'SENTINEL_MIG_0014', now(), true, now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id) "
                "VALUES ('US', 'SENTINEL_MIG_0014', 'SENT-0014')"
            )
        )
        leftover = conn.execute(
            text("SELECT count(*) FROM import_run_history WHERE import_type='BUSINESS_REPORT'")
        ).scalar_one()
        assert leftover == 0  # downgradeがBUSINESS_REPORTの履歴を片付けている

    _alembic(superuser_url, "upgrade", "head")  # 既存DBへ0014だけを適用
    with engine.connect() as conn:
        assert _tables(conn) == set(INCREMENTAL_RLS_TABLES)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016"
        assert conn.execute(text("SELECT count(*) FROM known_seller_master WHERE seller_id='SENTINEL_MIG_0014'")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM tool_execution_link WHERE tool_run_id='SENT-0014'")).scalar_one() == 1
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tool_execution_link WHERE tool_run_id='SENT-0014'"))
        conn.execute(text("DELETE FROM known_seller_master WHERE seller_id='SENTINEL_MIG_0014'"))
    engine.dispose()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_import_and_event(conn, event_type: str, note: str | None) -> int:
    conn.execute(
        text(
            "INSERT INTO import_run_history (import_run_id, marketplace, import_type, source_file_name, executed_at) "
            "VALUES ('US-BUSINESS_REPORT-m15-0001', 'US', 'BUSINESS_REPORT', 'm.csv', now()) ON CONFLICT DO NOTHING"
        )
    )
    import_id = conn.execute(
        text(
            "INSERT INTO business_report_import (marketplace, business_direction, market_basis, period_start, period_end, "
            "source_file_name, source_path, file_sha256, encoding_detected, header_text, import_run_id) VALUES "
            "('US','EXPORT','FOLDER','2998-01-01','2998-01-31','m.csv','m.csv', :sha, 'utf-8', 'h', 'US-BUSINESS_REPORT-m15-0001') "
            "RETURNING import_id"
        ),
        {"sha": "9" * 64},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO business_report_import_event (import_id, marketplace, event_type, note, actor) "
            "VALUES (:i, 'US', :t, :n, 'mig')"
        ),
        {"i": import_id, "t": event_type, "n": note},
    )
    return import_id


def test_0015_event_type_and_reason_constraints(superuser_url):
    engine = create_engine(superuser_url)
    try:
        for note in (None, "", "   "):  # 無効化は理由必須（DBでも強制）
            with pytest.raises(IntegrityError, match="ck_brie_deactivated_reason"):
                with engine.begin() as conn:
                    _insert_import_and_event(conn, "DEACTIVATED", note)
        with pytest.raises(IntegrityError, match="ck_brie_type"):
            with engine.begin() as conn:
                _insert_import_and_event(conn, "BOGUS", "x")
        with engine.begin() as conn:
            _insert_import_and_event(conn, "DEACTIVATED", "期間の指定誤り")  # 理由付きは成功
            conn.execute(text("DELETE FROM business_report_import_event WHERE actor='mig'"))
            conn.execute(text("DELETE FROM business_report_import WHERE source_path='m.csv'"))
            conn.execute(text("DELETE FROM import_run_history WHERE import_run_id='US-BUSINESS_REPORT-m15-0001'"))
    finally:
        engine.dispose()


def test_0015_upgrade_is_idempotent_when_rerun(superuser_url):
    module = _load(_MIGRATION_0015, "mig0015")
    engine = create_engine(superuser_url)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
    engine.dispose()


def test_0015_downgrade_keeps_events_as_superseded_and_reupgrades(superuser_url):
    engine = create_engine(superuser_url)
    with engine.begin() as conn:
        import_id = _insert_import_and_event(conn, "DEACTIVATED", "理由")
    _alembic(superuser_url, "downgrade", "0014")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014"
        assert conn.execute(
            text("SELECT event_type FROM business_report_import_event WHERE import_id=:i"), {"i": import_id}
        ).scalar_one() == "SUPERSEDED"  # 履歴は残り、種別の区別だけが失われる
        with pytest.raises(IntegrityError, match="ck_brie_type"):
            with conn.begin_nested():
                conn.execute(
                    text("INSERT INTO business_report_import_event (import_id, marketplace, event_type, note, actor) "
                         "VALUES (:i, 'US', 'DEACTIVATED', 'x', 'mig')"),
                    {"i": import_id},
                )
    _alembic(superuser_url, "upgrade", "head")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016"
        conn.execute(text("DELETE FROM business_report_import_event WHERE actor='mig'"))
        conn.execute(text("DELETE FROM business_report_import WHERE source_path='m.csv'"))
        conn.execute(text("DELETE FROM import_run_history WHERE import_run_id='US-BUSINESS_REPORT-m15-0001'"))
    engine.dispose()
