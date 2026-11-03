"""0016（出典管理）のスキーマ・RLS・権限・制約・移行の結合テスト（実PostgreSQL、使い捨てtest-db）。

BYPASSRLSの参照ロール(kpi_report_role)と通常ロール(app_role/admin_role)では期待値が異なる:
- app_role: 自分の市場の行だけ見える／追記専用表は書き換え不可／出典は列限定UPDATE
- kpi_report_role: 両市場が見える（BYPASSRLS）が、書き込みは一切不可
- admin_role: このテーブル群には権限なし
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
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from acquisition.registry import SourceRegistry
from db.table_names import ACQUISITION_BAND_RLS_TABLES, ACQUISITION_RLS_TABLES, ALL_TABLES
from tests.integration.acq_helpers import make_runs, new_source, unique_key
from tests.integration.conftest import role_url

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "alembic" / "versions" / "0016_acquisition_source_management.py"


def _alembic(superuser_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["SEEDASIN_DATABASE_URL"] = superuser_url
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, cwd=_ROOT, env=env)


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def owner(superuser_url):
    engine = create_engine(superuser_url)
    yield engine
    engine.dispose()


def test_fresh_build_has_tables_rls_policies_seed_trigger_and_not_in_all_tables(owner):
    assert not set(ACQUISITION_RLS_TABLES) & set(ALL_TABLES)  # 0003/0004が実行時に読むタプルに入れない
    with owner.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        rls = dict(conn.execute(text("SELECT relname, relrowsecurity FROM pg_class WHERE relname LIKE 'acquisition%' AND relkind = 'r'")).all())
        assert rls == {t: True for t in ACQUISITION_RLS_TABLES + ACQUISITION_BAND_RLS_TABLES}  # 0016の5表＋0019の価格帯
        pol = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_policies WHERE tablename LIKE 'acquisition%'"))}
        assert pol == set(ACQUISITION_RLS_TABLES + ACQUISITION_BAND_RLS_TABLES)
        seed = conn.execute(text("SELECT marketplace, source_type, min_interval_days, retry_cooldown_minutes, max_consecutive_failures, "
                                 "lease_seconds, captcha_lease_seconds, empty_counts_as_success, truncated_counts_as_success "
                                 "FROM acquisition_interval_config ORDER BY 1, 2")).all()
        assert len(seed) == 8 and {s.marketplace for s in seed} == {"US", "JP"}
        assert {(s.min_interval_days, s.retry_cooldown_minutes, s.max_consecutive_failures, s.lease_seconds, s.captcha_lease_seconds,
                 s.empty_counts_as_success, s.truncated_counts_as_success) for s in seed} == {(30, 60, 3, 300, 1800, True, True)}
        assert conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_collector_run_guard' AND NOT tgisinternal")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uk_collector_run_one_active_per_source'")).scalar_one() == 1


@pytest.mark.parametrize("table", ACQUISITION_RLS_TABLES)
def test_unset_marketplace_is_denied(superuser_url, table):
    engine = create_engine(role_url(superuser_url, "app_role"))
    try:
        with engine.connect() as conn:
            with pytest.raises(ProgrammingError, match="unrecognized configuration parameter"):
                conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    finally:
        engine.dispose()


def test_normal_roles_see_only_their_market_but_the_bypassrls_role_sees_both(app_role_factory, kpi_report_factory, registry):
    us = registry.register_source("US", "KEYWORD", unique_key("rls us "))
    jp = registry.register_source("JP", "KEYWORD", unique_key("rls jp "))
    with app_role_factory.get_session("US") as conn, conn.begin():
        ids = {r[0] for r in conn.execute(text("SELECT source_id FROM acquisition_source"))}
        events = {r[0] for r in conn.execute(text("SELECT source_id FROM acquisition_source_event"))}
        assert us.source_id in ids and jp.source_id not in ids and us.source_id in events and jp.source_id not in events
        assert {r[0] for r in conn.execute(text("SELECT marketplace FROM acquisition_interval_config"))} == {"US"}
    with app_role_factory.get_session("JP") as conn, conn.begin():
        ids = {r[0] for r in conn.execute(text("SELECT source_id FROM acquisition_source"))}
        assert jp.source_id in ids and us.source_id not in ids
    with kpi_report_factory.get_session() as conn:  # BYPASSRLS: 両市場が見える（期待値が異なる）
        ids = {r[0] for r in conn.execute(text("SELECT source_id FROM acquisition_source"))}
        assert {us.source_id, jp.source_id} <= ids
        assert {r[0] for r in conn.execute(text("SELECT marketplace FROM acquisition_interval_config"))} == {"US", "JP"}


def test_us_session_cannot_write_jp_rows(app_role_factory):
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="row-level security"):
            with conn.begin():
                conn.execute(text("INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary) "
                                  "VALUES ('JP','KEYWORD','x', :h, 'x')"), {"h": "a" * 64})


def test_kpi_report_role_is_read_only_and_admin_role_has_no_access(kpi_report_factory, admin_role_factory):
    for table in ACQUISITION_RLS_TABLES:
        with kpi_report_factory.get_session() as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(f"DELETE FROM {table}"))
        with admin_role_factory.get_session("US") as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(f"SELECT count(*) FROM {table}"))
    with kpi_report_factory.get_session() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(text("UPDATE acquisition_source SET status = 'ACTIVE'"))


def test_app_role_privileges_append_only_and_column_limited(app_role_factory, registry):
    s = new_source(registry)
    with app_role_factory.get_session("US") as conn:
        for sql in ("UPDATE acquisition_path SET note = 'x'", "DELETE FROM acquisition_path",
                    "UPDATE acquisition_source_event SET detail = 'x'", "DELETE FROM acquisition_source_event",
                    "DELETE FROM acquisition_source", "UPDATE acquisition_source SET key_primary = 'x'",
                    "UPDATE acquisition_source SET key_hash = repeat('b', 64)", "UPDATE acquisition_source SET marketplace = 'JP'",
                    "UPDATE acquisition_source SET source_type = 'KEYWORD'"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(sql))
        with conn.begin():  # 状態・保留・間隔の列だけ更新できる
            conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'MANUAL', interval_override_days = 7 WHERE source_id = :id"),
                         {"id": s.source_id})
        with conn.begin():  # 設定表は通常のCRUD
            conn.execute(text("UPDATE acquisition_interval_config SET min_interval_days = 30 WHERE marketplace = 'US'"))


def test_path_and_run_composite_fks_reject_a_mismatched_market_even_for_the_owner(owner, registry):
    s = new_source(registry)
    with owner.connect() as conn:  # RLSを受けない所有者接続で、FKそのものを検証
        with pytest.raises(IntegrityError, match="fk_ap_source"):
            with conn.begin():
                conn.execute(text(f"INSERT INTO acquisition_path (source_id, marketplace, stage, registered_by) VALUES ({s.source_id}, 'JP', 'MANUAL', 'x')"))
        with pytest.raises(IntegrityError, match="fk_ase_source"):
            with conn.begin():
                conn.execute(text(f"INSERT INTO acquisition_source_event (source_id, marketplace, event_type, actor) VALUES ({s.source_id}, 'JP', 'HOLD', 'x')"))


def test_constraints_on_source_config_and_path(app_role_factory, registry):
    s = new_source(registry)
    with app_role_factory.get_session("US") as conn:
        cases = [
            ("INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary) VALUES ('US','BOGUS','x', repeat('a',64), 'x')", "fk_as_config"),
            ("INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary) VALUES ('US','KEYWORD','', repeat('a',64), 'x')", "ck_as_key_primary"),
            (f"UPDATE acquisition_source SET status = 'ON_HOLD' WHERE source_id = {s.source_id}", "ck_as_hold_reason"),
            (f"UPDATE acquisition_source SET interval_override_days = 0 WHERE source_id = {s.source_id}", "ck_as_override"),
            ("UPDATE acquisition_interval_config SET min_interval_days = 0 WHERE marketplace = 'US'", "ck_aic_days"),
            (f"INSERT INTO acquisition_path (source_id, marketplace, stage, registered_by) VALUES ({s.source_id}, 'US', 'ORIGIN_ASIN', 'x')", "ck_ap_origin"),
            (f"INSERT INTO acquisition_path (source_id, marketplace, stage, registered_by) VALUES ({s.source_id}, 'JP', 'MANUAL', 'x')", "row-level security"),  # 別市場の行はRLSが先に拒否
            (f"INSERT INTO acquisition_source_event (source_id, marketplace, event_type, actor) VALUES ({s.source_id}, 'US', 'BOGUS', 'x')", "ck_ase_type"),
        ]
        for sql, constraint in cases:
            with pytest.raises((IntegrityError, DBAPIError), match=constraint):
                with conn.begin():
                    conn.execute(text(sql))
        with conn.begin():
            conn.execute(text(f"INSERT INTO acquisition_path (source_id, marketplace, stage, registered_by) VALUES ({s.source_id}, 'US', 'MANUAL', 'x')"))
        with pytest.raises(IntegrityError, match="uk_ap_identity"):  # 同一経路の重複はDBが拒否（NULLをすり抜けない）
            with conn.begin():
                conn.execute(text(f"INSERT INTO acquisition_path (source_id, marketplace, stage, registered_by) VALUES ({s.source_id}, 'US', 'MANUAL', 'y')"))


def test_collector_run_constraints_for_new_style_rows(app_role_factory, registry):
    s = new_source(registry)
    other_market = SourceRegistry(app_role_factory).register_source("JP", "KEYWORD", unique_key("jp "))
    base = ("INSERT INTO collector_run (marketplace, run_type, target, status, source_id, attempt_token, lease_expires_at, lease_seconds, outcome, interrupt_kind) "
            "VALUES ('US','X','t', {vals})")
    good = f"'RUNNING', {s.source_id}, gen_random_uuid(), now(), 300, NULL, NULL"
    with app_role_factory.get_session("US") as conn:
        bad = [
            (f"'RUNNING', {s.source_id}, NULL, now(), 300, NULL, NULL", "ck_collector_run_acq_fields"),  # トークン無し
            (f"'RUNNING', {s.source_id}, gen_random_uuid(), now(), 300, 'FAILED', NULL", "ck_collector_run_acq_outcome"),  # 実行中にoutcome
            (f"'COMPLETED', {s.source_id}, gen_random_uuid(), now(), 300, 'FAILED', NULL", "ck_collector_run_acq_outcome"),  # 状態と不整合
            (f"'FAILED', {s.source_id}, gen_random_uuid(), now(), 300, 'SUCCESS_NONEMPTY', NULL", "ck_collector_run_acq_outcome"),
            (f"'INTERRUPTED', {s.source_id}, gen_random_uuid(), now(), 300, 'INTERRUPTED', NULL", "ck_collector_run_acq_outcome"),  # 種別なし
            (f"'RUNNING', {other_market.source_id}, gen_random_uuid(), now(), 300, NULL, NULL", "fk_collector_run_source"),  # 別市場の出典
        ]
        for vals, constraint in bad:
            with pytest.raises((IntegrityError, DBAPIError), match=constraint):
                with conn.begin():
                    conn.execute(text(base.format(vals=vals)))
        with conn.begin():
            conn.execute(text(base.format(vals=good)))
        with pytest.raises(IntegrityError, match="uk_collector_run_one_active_per_source"):  # 同一出典の二重実行をDBが拒否
            with conn.begin():
                conn.execute(text(base.format(vals=good)))
        with conn.begin():  # 旧経路の行(source_id NULL)は従来どおり、制約の対象外で複数RUNNINGも可
            for _ in range(2):
                conn.execute(text("INSERT INTO collector_run (marketplace, run_type, target, status) VALUES ('US','BRAND_PAGE','legacy','RUNNING')"))


def test_downgrade_to_0015_and_upgrade_again_on_an_existing_db_preserves_legacy_rows(owner, superuser_url, app_role_factory):
    """既存DB(0015適用済み)へ0016だけを適用する経路。旧経路のcollector_run（旧RUNNINGを含む）が無傷で、
    新しい列はNULL、旧経路の更新が従来どおり動くこと。downgradeで新テーブルは消え、再upgradeできる。"""
    _alembic(superuser_url, "downgrade", "0015")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0015"
        assert conn.execute(text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'acquisition%'")).scalar_one() == 0
        cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'collector_run'"))}
        assert not {"source_id", "attempt_token", "lease_expires_at", "outcome"} & cols
        ids = [conn.execute(text("INSERT INTO collector_run (marketplace, run_type, target, status) VALUES ('US','BRAND_PAGE',:t,:s) RETURNING id"),
                            {"t": f"https://www.amazon.com/s?k=mig{i}", "s": st}).scalar_one()
               for i, st in enumerate(("RUNNING", "COMPLETED", "FAILED", "CAPTCHA_WAIT"))]
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        rows = conn.execute(text("SELECT id, status, source_id, attempt_token, lease_expires_at, outcome FROM collector_run WHERE id = ANY(:ids) ORDER BY id"),
                            {"ids": ids}).all()
        assert [r.status for r in rows] == ["RUNNING", "COMPLETED", "FAILED", "CAPTCHA_WAIT"]
        assert all(r.source_id is None and r.attempt_token is None and r.lease_expires_at is None and r.outcome is None for r in rows)
    with app_role_factory.get_session("US") as conn, conn.begin():  # 旧RUNNINGは従来どおり更新できる
        conn.execute(text("UPDATE collector_run SET status = 'FAILED' WHERE id = :id"), {"id": ids[0]})
        conn.execute(text("DELETE FROM collector_run WHERE id = ANY(:ids)"), {"ids": ids})


def test_downgrade_maps_interrupted_to_failed_and_drops_new_columns(owner, superuser_url, app_role_factory, registry):
    runs = make_runs(app_role_factory)
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    runs.interrupt(c, "USER")
    _alembic(superuser_url, "downgrade", "0015")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT status FROM collector_run WHERE id = :id"), {"id": c.run_id}).scalar_one() == "FAILED"
        with pytest.raises(IntegrityError, match="ck_collector_run_status"):
            with conn.begin_nested():
                conn.execute(text("UPDATE collector_run SET status = 'INTERRUPTED' WHERE id = :id"), {"id": c.run_id})
        conn.execute(text("DELETE FROM collector_asin WHERE collector_run_id = :id"), {"id": c.run_id})
        conn.execute(text("DELETE FROM collector_run WHERE id = :id"), {"id": c.run_id})
    _alembic(superuser_url, "upgrade", "head")


def test_upgrade_function_is_idempotent_when_rerun(owner):
    spec = importlib.util.spec_from_file_location("mig0016", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with owner.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        n = conn.execute(text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'acquisition%'")).scalar_one()
        seed = conn.execute(text("SELECT count(*) FROM acquisition_interval_config")).scalar_one()
    assert (n, seed) == (6, 8)  # 再実行でも重複しない（0016の5表＋0019の価格帯1表）


def test_downgrade_keeps_every_collector_run_and_collector_asin_row(owner, superuser_url, app_role_factory, registry):
    """0016のdowngradeは既存のcollector_run・collector_asinを削除しない（新方式の行も残る）。
    失われるのは、acquisition_*テーブルの全データと、collector_runの新規列（source_id・lease・outcome等）。
    INTERRUPTEDは意味を失ってFAILEDに読み替えられる。"""
    runs = make_runs(app_role_factory)
    with app_role_factory.get_session("US") as conn, conn.begin():
        legacy_id = conn.execute(text("INSERT INTO collector_run (marketplace, run_type, target, status, asin_count) "
                                      "VALUES ('US','BRAND_PAGE','https://www.amazon.com/s?k=keep','COMPLETED', 2) RETURNING id")).scalar_one()
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :id, 'B0KEEPASN1'), ('US', :id, 'B0KEEPASN2')"),
                     {"id": legacy_id})
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    runs.append_asins(c, ["B0NEWSTY01", "B0NEWSTY02", "B0NEWSTY03"])
    runs.interrupt(c, "USER")

    def count(table):
        with owner.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()

    before = (count("collector_run"), count("collector_asin"))
    _alembic(superuser_url, "downgrade", "0015")
    after = (count("collector_run"), count("collector_asin"))
    assert after == before  # 1行も削除されない
    with owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :id"), {"id": c.run_id}).scalar_one() == 3
        assert conn.execute(text("SELECT status FROM collector_run WHERE id = :id"), {"id": c.run_id}).scalar_one() == "FAILED"
        assert conn.execute(text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'acquisition%'")).scalar_one() == 0  # 失われるもの
    _alembic(superuser_url, "upgrade", "head")
    assert (count("collector_run"), count("collector_asin")) == before
    with owner.connect() as conn:  # 再upgrade後、旧行は新方式の列がNULLの旧経路の行として残る
        assert conn.execute(text("SELECT source_id IS NULL FROM collector_run WHERE id = :id"), {"id": c.run_id}).scalar_one() is True


# ---------------------------------------------------------------- 0017（続き取得のcycle）の移行

def test_0017_upgrade_from_0016_keeps_existing_runs_untouched_and_downgrade_keeps_every_row(owner, superuser_url, app_role_factory, registry):
    """既存リビジョン(0016)の新方式のrun（SUCCESS_TRUNCATED）に、0017を適用してもcycle列はNULLのまま・行は無傷。
    旧runは再開できるものとして扱わず、従来の30日制限のまま。0017のdowngradeは行を削除せず、再upgradeできる。"""
    from acquisition.eligibility import db_now, decide, load_configs, load_facts

    s = new_source(registry)
    _alembic(superuser_url, "downgrade", "0016")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016"
        cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'collector_run'"))}
        assert not {"cycle_id", "next_url", "cycle_state"} & cols
        rid = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, outcome, completeness, truncation_reason, source_id, attempt_token, "
            "lease_expires_at, lease_seconds, completed_at, pages_fetched, asin_count) "
            "VALUES ('US', 'SELLER_STOREFRONT', 't0016', 'COMPLETED', 'SUCCESS_TRUNCATED', 'TRUNCATED', 'PAGE_LIMIT', :sid, gen_random_uuid(), "
            "now(), 300, now() - interval '1 day', 2, 32) RETURNING id"), {"sid": s.source_id}).scalar_one()
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :r, 'B0OLDRUN01'), ('US', :r, 'B0OLDRUN02')"), {"r": rid})
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        r = conn.execute(text("SELECT status, outcome, truncation_reason, cycle_id, cycle_seq, cycle_state, next_url, cursor_error FROM collector_run WHERE id = :i"),
                         {"i": rid}).one()
        assert (r.status, r.outcome, r.truncation_reason) == ("COMPLETED", "SUCCESS_TRUNCATED", "PAGE_LIMIT")
        assert (r.cycle_id, r.cycle_seq, r.cycle_state, r.next_url, r.cursor_error) == (None, None, None, None, None)
        assert conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :i"), {"i": rid}).scalar_one() == 2
        cfgrow = conn.execute(text("SELECT cycle_max_age_days, overlap_pages, max_cycle_restarts FROM acquisition_interval_config WHERE marketplace = 'US' AND source_type = 'SELLER'")).one()
        assert tuple(cfgrow) == (14, 1, 2)  # 承認された初期値
    with app_role_factory.get_session("US") as conn, conn.begin():
        f = load_facts(conn, "US", [s.source_id])[0]
        d = decide(f, load_configs(conn, "US")[f.source_type], db_now(conn))
    assert d.state == "TOO_RECENT" and d.plan == "FRESH" and d.cycle is None  # 旧runは30日制限のまま・続き取得の対象外
    # downgrade: 行は残り、追加列と制約・トリガーが消える。保護トリガーは0016の定義に戻る
    with owner.begin() as conn:
        before = (conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one(), conn.execute(text("SELECT count(*) FROM collector_asin")).scalar_one())
    _alembic(superuser_url, "downgrade", "0016")
    with owner.begin() as conn:
        after = (conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one(), conn.execute(text("SELECT count(*) FROM collector_asin")).scalar_one())
        assert after == before
        cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'collector_run'"))}
        assert not {"cycle_id", "next_url", "cycle_state", "continues_run_id"} & cols
        assert conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_collector_run_cycle_init'")).scalar_one() == 0
        with pytest.raises(IntegrityError, match="terminal"):
            with conn.begin_nested():
                conn.execute(text("UPDATE collector_run SET pages_fetched = 9 WHERE id = :i"), {"i": rid})  # 0016の保護は維持
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        assert conn.execute(text("SELECT cycle_id FROM collector_run WHERE id = :i"), {"i": rid}).scalar_one() is None


def test_0017_upgrade_function_is_idempotent_when_rerun(owner, superuser_url):
    # 0019の価格帯表は0017の一意制約(uk_collector_run_ref等)を複合外部キーで参照するため、0017の関数をheadの上で直接再実行すると
    # その制約を作り直せない。0018まで戻して（価格帯表は空なので戻せる）から再実行し、終わったらheadへ戻す。
    _alembic(superuser_url, "downgrade", "0018")
    path = _ROOT / "alembic" / "versions" / "0017_collector_run_cycles.py"
    spec = importlib.util.spec_from_file_location("mig0017", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with owner.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        n = conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_collector_run_cycle_init', 'trg_collector_run_guard')")).scalar_one()
    assert n == 2
    _alembic(superuser_url, "upgrade", "head")


# ---------------------------------------------------------------- 0018（続き取得のレビュー対応）の移行

def test_0018_upgrade_from_0017_keeps_rows_and_an_existing_overlap_above_one_is_not_rewritten(owner, superuser_url, app_role_factory, registry):
    """0017適用済みの環境（test-db等）へ0018を増分で適用する。既存のrunは無傷でend_reachedはNULL（=末尾到達の記録なし）。
    既に2以上が入っていたoverlap_pagesは書き換えず（制約はNOT VALID）、アプリが設定エラーとして止める。0/1へ直せば検証済みの制約にできる。"""
    from acquisition.eligibility import ConfigError, load_configs

    s = new_source(registry)
    runs = make_runs(app_role_factory)
    c = runs.claim("US", s.source_id)
    runs.finish(c, "SUCCESS_TRUNCATED", truncation_reason="PAGE_LIMIT")  # 位置なしの打切り（CLOSED_NO_CURSOR）
    _alembic(superuser_url, "downgrade", "0017")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0017"
        cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'collector_run'"))}
        assert "end_reached" not in cols
        conn.execute(text("UPDATE acquisition_interval_config SET overlap_pages = 3 WHERE marketplace = 'US' AND source_type = 'KEYWORD'"))
        before = conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one()
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        assert conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one() == before
        r = conn.execute(text("SELECT status, cycle_state, end_reached FROM collector_run WHERE id = :i"), {"i": c.run_id}).one()
        assert tuple(r) == ("COMPLETED", "CLOSED_NO_CURSOR", None)
        assert conn.execute(text("SELECT overlap_pages FROM acquisition_interval_config WHERE marketplace = 'US' AND source_type = 'KEYWORD'")).scalar_one() == 3
        assert conn.execute(text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_aic_overlap_pages'")).scalar_one() is False
    with app_role_factory.get_session("US") as conn, conn.begin():
        with pytest.raises(ConfigError, match="overlap_pages=3"):
            load_configs(conn, "US")
        conn.execute(text("UPDATE acquisition_interval_config SET overlap_pages = 1 WHERE marketplace = 'US' AND source_type = 'KEYWORD'"))
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE acquisition_interval_config VALIDATE CONSTRAINT ck_aic_overlap_pages"))
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(text("UPDATE acquisition_interval_config SET overlap_pages = 2 WHERE marketplace = 'US' AND source_type = 'SELLER'"))


def test_0018_downgrade_without_daily_limit_rows_restores_the_0017_constraints(owner, superuser_url):
    with owner.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM collector_run WHERE interrupt_kind = 'DAILY_LIMIT'")).scalar_one() == 0  # この順で先に実行する
    _alembic(superuser_url, "downgrade", "0017")
    with owner.begin() as conn:
        assert "DAILY_LIMIT" not in conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_collector_run_interrupt_values'")).scalar_one()
        assert "3" in conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_aic_overlap_pages'")).scalar_one()
    _alembic(superuser_url, "upgrade", "head")


def test_0018_downgrade_keeps_daily_limit_rows_unchanged_and_can_be_reapplied(owner, superuser_url, app_role_factory, registry):
    s = new_source(registry)
    runs = make_runs(app_role_factory)
    c = runs.claim("US", s.source_id)
    assert runs.interrupt(c, "DAILY_LIMIT", "日次枠")
    with owner.begin() as conn:
        before = conn.execute(text("SELECT status, outcome, interrupt_kind, error_message FROM collector_run WHERE id = :i"), {"i": c.run_id}).one()
        n = conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one()
    _alembic(superuser_url, "downgrade", "0017")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one() == n
        assert conn.execute(text("SELECT status, outcome, interrupt_kind, error_message FROM collector_run WHERE id = :i"), {"i": c.run_id}).one() == before
        assert "DAILY_LIMIT" in conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_collector_run_interrupt_values'")).scalar_one()
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
        assert conn.execute(text("SELECT interrupt_kind FROM collector_run WHERE id = :i"), {"i": c.run_id}).scalar_one() == "DAILY_LIMIT"


def test_0018_upgrade_function_is_idempotent_when_rerun(owner):
    path = _ROOT / "alembic" / "versions" / "0018_continuation_followups.py"
    spec = importlib.util.spec_from_file_location("mig0018", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with owner.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        n = conn.execute(text("SELECT count(*) FROM pg_constraint WHERE conname IN ('ck_collector_run_end_reached', 'ck_collector_run_interrupt_values', 'ck_aic_overlap_pages')")).scalar_one()
    assert n == 3


def test_0018_rollback_past_0017_with_daily_limit_rows_keeps_every_row_and_upgrades_again(owner, superuser_url, app_role_factory, registry):
    """DAILY_LIMITの中断行がある状態で、0016・0015まで戻しても行は削除されず、再びheadへ上げられる。
    0016へは行を変更せずに戻る（interrupt_kindの制約はDAILY_LIMITを許す定義のまま残る）。0015へ戻す際は、0016のdowngradeの既存の規則
    （中断を失敗へ写し、0016の列を外す）がこの行にも同じく適用される（0018の変更ではない）。"""
    s = new_source(registry)
    runs = make_runs(app_role_factory)
    c = runs.claim("US", s.source_id)
    assert runs.interrupt(c, "DAILY_LIMIT", "日次枠")
    count = lambda conn: conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one()
    with owner.begin() as conn:
        n = count(conn)
    _alembic(superuser_url, "downgrade", "0016")
    with owner.begin() as conn:
        assert count(conn) == n
        assert tuple(conn.execute(text("SELECT status, interrupt_kind FROM collector_run WHERE id = :i"), {"i": c.run_id}).one()) == ("INTERRUPTED", "DAILY_LIMIT")
        assert "DAILY_LIMIT" in conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_collector_run_interrupt_values'")).scalar_one()
    _alembic(superuser_url, "downgrade", "0015")
    with owner.begin() as conn:
        assert count(conn) == n
        assert conn.execute(text("SELECT status FROM collector_run WHERE id = :i"), {"i": c.run_id}).scalar_one() == "FAILED"  # 0016のdowngradeの既存規則
    _alembic(superuser_url, "upgrade", "head")
    with owner.begin() as conn:
        assert count(conn) == n and conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019"
