"""0019（acquisition_band: 価格帯の台帳）の結合テスト（実PostgreSQL、使い捨てtest-db）。

- マイグレーション: 再実行の冪等性、取得途中(PENDING/ACTIVE)の帯が残るときのdowngrade中止と、全て終端のときの成功
- 保護トリガー: 識別列の不変、状態の一方向遷移、終端の不変、一巡にACTIVEは1つ、挿入の規則（PENDINGのみ・親はSPLIT・重ならない）
- RLS・権限: app/kpi/adminの3ロール（未設定の拒否、市場の分離、DELETE・範囲外列UPDATEの拒否）
- lease のメソッド: init_bands（冪等）、advance_band（分割・遷移）、lease喪失で書けない、append_asinsの帯カウンタ、finishのcompleteness
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from acquisition.lease import LeaseLost, PageCursor
from db.table_names import ACQUISITION_BAND_RLS_TABLES, ALL_TABLES
from tests.integration.acq_helpers import expire_run, make_runs, new_source, run_row
from tests.integration.conftest import role_url

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "alembic" / "versions" / "0019_acquisition_band.py"


def _alembic(superuser_url: str, *args: str, check: bool = True):
    env = os.environ.copy()
    env["SEEDASIN_DATABASE_URL"] = superuser_url
    return subprocess.run([sys.executable, "-m", "alembic", *args], check=check, cwd=_ROOT, env=env, capture_output=True, text=True)


@pytest.fixture()
def owner(superuser_url):
    engine = create_engine(superuser_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def registry(app_role_factory):
    from acquisition.registry import SourceRegistry
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


@pytest.fixture()
def claim(runs, registry):
    return runs.claim("US", new_source(registry).source_id)


def _insert(owner, claim, lo, hi, *, state="PENDING", parent=None, depth=0, market=None):
    with owner.begin() as conn:
        return conn.execute(
            text("INSERT INTO acquisition_band (marketplace, source_id, cycle_id, lo_units, hi_units, parent_band_id, depth, state) "
                 "VALUES (:m, :s, :c, :lo, :hi, :p, :d, :st) RETURNING band_id"),
            {"m": market or claim.marketplace, "s": claim.source_id, "c": claim.cycle_id, "lo": lo, "hi": hi, "p": parent, "d": depth, "st": state},
        ).scalar_one()


def _owner_update(owner, sql, **params):
    with owner.begin() as conn:
        conn.execute(text(sql), params)


def _states(runs, claim):
    return {b.band_id: b for b in runs.load_bands(claim)}


# ---------------------------------------------------------------- マイグレーション

def test_fresh_build_has_table_rls_policy_triggers_and_is_not_in_all_tables(owner):
    assert not set(ACQUISITION_BAND_RLS_TABLES) & set(ALL_TABLES)
    with owner.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
        assert conn.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'acquisition_band'")).scalar_one() is True
        assert conn.execute(text("SELECT count(*) FROM pg_policies WHERE tablename = 'acquisition_band'")).scalar_one() == 1
        trg = {r[0] for r in conn.execute(text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'acquisition_band'::regclass AND NOT tgisinternal"))}
        assert trg == {"trg_acquisition_band_guard", "trg_acquisition_band_insert_guard"}
        idx = {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'acquisition_band'"))}
        assert {"uk_band_range", "uk_band_one_active", "ix_band_cycle_state"} <= idx


def test_upgrade_function_is_idempotent_when_rerun(owner):
    spec = importlib.util.spec_from_file_location("mig0019", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    with owner.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        assert conn.execute(text("SELECT count(*) FROM pg_policies WHERE tablename = 'acquisition_band'")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgrelid = 'acquisition_band'::regclass AND NOT tgisinternal")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'acquisition_band'::regclass AND contype = 'f'")).scalar_one() == 5


def test_downgrade_is_refused_while_a_band_is_unfinished_and_succeeds_when_all_are_terminal(owner, superuser_url, runs, claim):
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE acquisition_band"))  # このテストだけで判定するため、他テストの帯を空にする（所有者）
    runs.init_bands(claim, [(0, 1000), (1000, None)])
    res = _alembic(superuser_url, "downgrade", "0018", check=False)
    assert res.returncode != 0 and "downgradeを中止" in (res.stderr + res.stdout) and "PENDING/ACTIVE" in (res.stderr + res.stdout)
    with owner.connect() as conn:  # 何も変更されていない
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
        assert conn.execute(text("SELECT count(*) FROM acquisition_band")).scalar_one() == 2
    bands = runs.load_bands(claim)
    for b in bands:  # 全て終端にする（1つずつ開いて閉じる）
        runs.advance_band(claim, b.band_id, "ACTIVE")
        runs.advance_band(claim, b.band_id, "DONE", close_reason="END_REACHED")
    res = _alembic(superuser_url, "downgrade", "0018", check=False)
    assert res.returncode == 0, res.stderr
    with owner.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0018"
        assert conn.execute(text("SELECT to_regclass('acquisition_band')")).scalar_one() is None
        assert conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'acquisition_band%'")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM collector_run")).scalar_one() > 0  # collector_runは無傷
    _alembic(superuser_url, "upgrade", "head")
    with owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM acquisition_band")).scalar_one() == 0


# ---------------------------------------------------------------- 保護トリガー・制約

def test_identity_columns_are_immutable(owner, claim):
    b = _insert(owner, claim, 0, 100)
    with pytest.raises(IntegrityError, match="identity columns are immutable"):
        _owner_update(owner, "UPDATE acquisition_band SET lo_units = 1 WHERE band_id = :b", b=b)
    with pytest.raises(IntegrityError, match="identity columns are immutable"):
        _owner_update(owner, "UPDATE acquisition_band SET hi_units = 200 WHERE band_id = :b", b=b)
    with pytest.raises(IntegrityError, match="identity columns are immutable"):
        _owner_update(owner, "UPDATE acquisition_band SET created_at = now() - interval '1 day' WHERE band_id = :b", b=b)


def test_state_machine_is_one_way_and_terminal_rows_are_frozen(owner, claim):
    b = _insert(owner, claim, 0, 100)
    # PENDINGからDONE/SPLIT/TRUNCATED_MINへは直行できない
    for st in ("DONE", "SPLIT", "TRUNCATED_MIN"):
        with pytest.raises(IntegrityError, match="cannot change state"):
            _owner_update(owner, "UPDATE acquisition_band SET state = :st, open_run_id = :r, close_run_id = :r, closed_at = now(), close_reason = 'x' "
                                 "WHERE band_id = :b", st=st, r=claim.run_id, b=b)
    _owner_update(owner, "UPDATE acquisition_band SET state = 'ACTIVE', open_run_id = :r WHERE band_id = :b", r=claim.run_id, b=b)
    with pytest.raises(IntegrityError, match="cannot change state"):  # 逆戻り
        _owner_update(owner, "UPDATE acquisition_band SET state = 'PENDING', open_run_id = NULL WHERE band_id = :b", b=b)
    with pytest.raises(IntegrityError, match="open_run_id is set once"):
        _owner_update(owner, "UPDATE acquisition_band SET open_run_id = NULL WHERE band_id = :b", b=b)
    _owner_update(owner, "UPDATE acquisition_band SET pages_fetched = 3, asins_new = 40 WHERE band_id = :b", b=b)
    with pytest.raises(IntegrityError, match="counters cannot decrease"):
        _owner_update(owner, "UPDATE acquisition_band SET pages_fetched = 2 WHERE band_id = :b", b=b)
    _owner_update(owner, "UPDATE acquisition_band SET state = 'DONE', close_run_id = :r, closed_at = now(), close_reason = 'END_REACHED' WHERE band_id = :b",
                  r=claim.run_id, b=b)
    for sql in ("UPDATE acquisition_band SET pages_fetched = 9 WHERE band_id = :b",
                "UPDATE acquisition_band SET state = 'ACTIVE' WHERE band_id = :b",
                "UPDATE acquisition_band SET close_reason = 'other' WHERE band_id = :b"):
        with pytest.raises(IntegrityError, match="already terminal"):
            _owner_update(owner, sql, b=b)


def test_only_one_active_band_per_cycle(owner, claim):
    a, b = _insert(owner, claim, 0, 100), _insert(owner, claim, 100, None)
    _owner_update(owner, "UPDATE acquisition_band SET state = 'ACTIVE', open_run_id = :r WHERE band_id = :b", r=claim.run_id, b=a)
    with pytest.raises(IntegrityError, match="uk_band_one_active"):
        _owner_update(owner, "UPDATE acquisition_band SET state = 'ACTIVE', open_run_id = :r WHERE band_id = :b", r=claim.run_id, b=b)


def test_insert_rules_pending_only_parent_split_no_overlap_and_shape_checks(owner, claim):
    with pytest.raises(IntegrityError, match="must start as PENDING"):
        _insert(owner, claim, 0, 10, state="DONE")
    root = _insert(owner, claim, 0, 100)
    with pytest.raises(IntegrityError, match="overlaps"):
        _insert(owner, claim, 50, 150)
    with pytest.raises(IntegrityError, match="overlaps"):
        _insert(owner, claim, 99, None)
    with pytest.raises(IntegrityError, match="overlaps"):
        _insert(owner, claim, 0, 100)  # 同じ範囲の重複（重なり検査が先に拒否する。一意索引 uk_band_range は最後の砦）
    with pytest.raises(IntegrityError):  # 親がSPLITでない
        _insert(owner, claim, 0, 50, parent=root, depth=1)
    with pytest.raises(IntegrityError, match="ck_band_range"):
        _insert(owner, claim, 200, 200)  # hi > lo
    with pytest.raises(IntegrityError, match="ck_band_lo"):
        _insert(owner, claim, -1, 0)
    with pytest.raises(IntegrityError, match="ck_band_depth"):
        _insert(owner, claim, 300, 400, depth=1)  # 親なしで depth>0
    # 他の一巡・他の出典の組み合わせはFKで拒否される
    with pytest.raises(IntegrityError, match="fk_band_source|fk_band_cycle"):
        with owner.begin() as conn:
            conn.execute(text("INSERT INTO acquisition_band (marketplace, source_id, cycle_id, lo_units, hi_units) VALUES ('US', :s, :c, 900, 1000)"),
                         {"s": claim.source_id + 12345, "c": claim.cycle_id})
    with pytest.raises(IntegrityError, match="fk_band_source|fk_band_cycle"):  # 取得先市場が一巡と食い違う
        _insert(owner, claim, 1100, 1200, market="JP")


# ---------------------------------------------------------------- RLS・権限（3ロール）

@pytest.mark.parametrize("table", ACQUISITION_BAND_RLS_TABLES)
def test_unset_marketplace_is_denied(superuser_url, table):
    engine = create_engine(role_url(superuser_url, "app_role"))
    try:
        with engine.connect() as conn:
            with pytest.raises(ProgrammingError, match="unrecognized configuration parameter"):
                conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    finally:
        engine.dispose()


def test_markets_are_isolated_and_roles_have_the_expected_privileges(owner, runs, registry, claim, app_role_factory, kpi_report_factory, admin_role_factory):
    us = claim
    jp = runs.claim("JP", new_source(registry, "JP").source_id)
    runs.init_bands(us, [(0, 500)])
    runs.init_bands(jp, [(0, 700)])
    us_ids = {b.band_id for b in runs.load_bands(us)}
    jp_ids = {b.band_id for b in runs.load_bands(jp)}
    with app_role_factory.get_session("US") as conn, conn.begin():
        seen = {r[0] for r in conn.execute(text("SELECT band_id FROM acquisition_band"))}
        assert us_ids <= seen and not jp_ids & seen
    with app_role_factory.get_session("JP") as conn, conn.begin():
        seen = {r[0] for r in conn.execute(text("SELECT band_id FROM acquisition_band"))}
        assert jp_ids <= seen and not us_ids & seen
    with kpi_report_factory.get_session() as conn:  # BYPASSRLS: 両市場が見える（期待値が異なる）。書き込みは不可
        with conn.begin():
            seen = {r[0] for r in conn.execute(text("SELECT band_id FROM acquisition_band"))}
        assert (us_ids | jp_ids) <= seen
        for sql in ("DELETE FROM acquisition_band", "UPDATE acquisition_band SET pages_fetched = 1",
                    "INSERT INTO acquisition_band (marketplace, source_id, cycle_id, lo_units) VALUES ('US', 1, 1, 0)"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(sql))
    with admin_role_factory.get_session("US") as conn:  # admin_role には付与なし
        with pytest.raises(DBAPIError, match="permission denied"):
            with conn.begin():
                conn.execute(text("SELECT count(*) FROM acquisition_band"))
    with app_role_factory.get_session("US") as conn:
        for sql in ("DELETE FROM acquisition_band",                                  # DELETEなし
                    "UPDATE acquisition_band SET lo_units = 1", "UPDATE acquisition_band SET hi_units = 9",
                    "UPDATE acquisition_band SET marketplace = 'JP'", "UPDATE acquisition_band SET cycle_id = 1",
                    "UPDATE acquisition_band SET source_id = 1", "UPDATE acquisition_band SET parent_band_id = 1",
                    "UPDATE acquisition_band SET depth = 1", "UPDATE acquisition_band SET axis = 'PRICE'",
                    "UPDATE acquisition_band SET created_at = now()"):               # 範囲外の列
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(sql))
        with pytest.raises(DBAPIError, match="row-level security"):  # 他市場の行は挿入できない
            with conn.begin():
                conn.execute(text("INSERT INTO acquisition_band (marketplace, source_id, cycle_id, lo_units, hi_units) VALUES ('JP', :s, :c, 5000, 6000)"),
                             {"s": jp.source_id, "c": jp.cycle_id})
        with conn.begin():  # 許可された列の更新は通る（US側）。JPの行は見えないので0行
            n = conn.execute(text("UPDATE acquisition_band SET pages_fetched = pages_fetched WHERE marketplace = 'JP'")).rowcount
            assert n == 0


# ---------------------------------------------------------------- lease のメソッド

def test_init_bands_is_idempotent_and_validates(runs, claim):
    first = runs.init_bands(claim, [(0, 1000), (1000, 5000), (5000, None)])
    assert [(b.lo_units, b.hi_units, b.state, b.depth) for b in first] == [(0, 1000, "PENDING", 0), (1000, 5000, "PENDING", 0), (5000, None, "PENDING", 0)]
    again = runs.init_bands(claim, [(0, 1000), (1000, 5000), (5000, None)])
    assert [b.band_id for b in again] == [b.band_id for b in first]  # 行が増えない
    other = runs.init_bands(claim, [(0, None)])  # 既に帯がある一巡では、別の指定でも何も作らない
    assert [b.band_id for b in other] == [b.band_id for b in first]
    for bad in ([], [(0, 0)], [(10, 5)], [(0, None), (5, 9)], [(0, 10), (5, 20)], [(-1, 5)]):
        with pytest.raises(ValueError):
            runs.init_bands(claim, bad)


def test_advance_band_activate_close_and_split(runs, claim):
    runs.init_bands(claim, [(0, 1000), (1000, None)])
    low, high = runs.load_bands(claim)
    out = {b.band_id: b for b in runs.advance_band(claim, low.band_id, "ACTIVE")}
    assert out[low.band_id].state == "ACTIVE" and out[low.band_id].open_run_id == claim.run_id
    with pytest.raises(ValueError, match="既にACTIVE"):
        runs.advance_band(claim, high.band_id, "ACTIVE")
    with pytest.raises(ValueError, match="close_reason"):
        runs.advance_band(claim, low.band_id, "TRUNCATED_MIN")
    with pytest.raises(ValueError, match="split_at"):
        runs.advance_band(claim, low.band_id, "SPLIT", split_at=1000)  # 帯の内側でない
    with pytest.raises(ValueError, match="split_at"):
        runs.advance_band(claim, low.band_id, "DONE", close_reason="x", split_at=500)
    with pytest.raises(ValueError):
        runs.advance_band(claim, high.band_id, "DONE", close_reason="x")  # PENDINGから直行は不可
    assert _states(runs, claim)[low.band_id].state == "ACTIVE"  # 失敗した呼び出しは何も変えない

    out = runs.advance_band(claim, low.band_id, "SPLIT", split_at=400)
    by = {b.band_id: b for b in out}
    parent = by[low.band_id]
    assert (parent.state, parent.close_reason, parent.close_run_id) == ("SPLIT", "SPLIT", claim.run_id)
    kids = sorted((b for b in out if b.parent_band_id == low.band_id), key=lambda b: b.lo_units)
    assert [(k.lo_units, k.hi_units, k.depth) for k in kids] == [(0, 400, 1), (400, 1000, 1)]
    assert [k.state for k in kids] == ["ACTIVE", "PENDING"] and kids[0].open_run_id == claim.run_id  # 左がACTIVE
    assert sum(1 for b in out if b.state == "ACTIVE") == 1

    right = kids[1]
    runs.advance_band(claim, kids[0].band_id, "DONE", close_reason="END_REACHED")
    runs.advance_band(claim, right.band_id, "ACTIVE")
    out = {b.band_id: b for b in runs.advance_band(claim, right.band_id, "TRUNCATED_MIN", close_reason="MIN_WIDTH")}
    assert (out[right.band_id].state, out[right.band_id].close_reason) == ("TRUNCATED_MIN", "MIN_WIDTH")
    with pytest.raises(ValueError):  # 終端からは動かせない
        runs.advance_band(claim, right.band_id, "ACTIVE")
    # 上限なしの帯も分割できる（右の子は hi=None）
    runs.advance_band(claim, high.band_id, "ACTIVE")
    out = runs.advance_band(claim, high.band_id, "SPLIT", split_at=2000, close_reason="LIMIT")
    kids = sorted((b for b in out if b.parent_band_id == high.band_id), key=lambda b: b.lo_units)
    assert [(k.lo_units, k.hi_units) for k in kids] == [(1000, 2000), (2000, None)]


def test_another_cycles_band_cannot_be_advanced(runs, registry, claim):
    other = runs.claim("US", new_source(registry).source_id)
    runs.init_bands(other, [(0, None)])
    foreign = runs.load_bands(other)[0]
    with pytest.raises(ValueError, match="存在しません"):
        runs.advance_band(claim, foreign.band_id, "ACTIVE")


def test_a_run_that_lost_its_lease_cannot_read_or_write_bands(runs, claim, owner):
    runs.init_bands(claim, [(0, 100), (100, None)])
    first = runs.load_bands(claim)[0]
    runs.advance_band(claim, first.band_id, "ACTIVE")
    expire_run(runs._app, "US", claim.run_id)
    with pytest.raises(LeaseLost):
        runs.load_bands(claim)
    with pytest.raises(LeaseLost):
        runs.init_bands(claim, [(200, 300)])
    with pytest.raises(LeaseLost):
        runs.advance_band(claim, first.band_id, "SPLIT", split_at=50)
    with pytest.raises(LeaseLost):
        runs.append_asins(claim, ["B0LOSTLEAS"], band_id=first.band_id)
    with owner.connect() as conn:  # 何も書かれていない
        rows = conn.execute(text("SELECT state, pages_fetched, asins_new FROM acquisition_band WHERE cycle_id = :c ORDER BY lo_units"),
                            {"c": claim.cycle_id}).all()
        assert [tuple(r) for r in rows] == [("ACTIVE", 0, 0), ("PENDING", 0, 0)]
        assert conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :r"), {"r": claim.run_id}).scalar_one() == 0


def test_append_asins_updates_the_band_counters_in_the_same_transaction(runs, claim, owner):
    runs.init_bands(claim, [(0, None)])
    band = runs.load_bands(claim)[0]
    cursor = PageCursor("https://x/p2", 2, "https://x/p1", "a" * 64, ("B0BANDAAA1", "B0BANDAAA2"))
    with pytest.raises(ValueError, match="ACTIVE"):  # PENDINGの帯には書けない: ASIN・カーソル・ページ数も全て取り消される
        runs.append_asins(claim, ["B0BANDAAA1", "B0BANDAAA2"], cursor=cursor, band_id=band.band_id)
    with owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM collector_asin WHERE collector_run_id = :r"), {"r": claim.run_id}).scalar_one() == 0
        assert conn.execute(text("SELECT pages_fetched, next_url FROM collector_run WHERE id = :r"), {"r": claim.run_id}).one() == (None, None)
    runs.advance_band(claim, band.band_id, "ACTIVE")
    runs.append_asins(claim, ["B0BANDAAA1", "B0BANDAAA2"], cursor=cursor, band_id=band.band_id)
    runs.append_asins(claim, ["B0BANDAAA3"], band_id=band.band_id)
    b = runs.load_bands(claim)[0]
    assert (b.pages_fetched, b.asins_new) == (2, 3)
    with owner.connect() as conn:
        assert conn.execute(text("SELECT pages_fetched, asin_count, next_page_no FROM collector_run WHERE id = :r"), {"r": claim.run_id}).one() == (2, 3, 2)
    with pytest.raises(ValueError):  # 存在しない帯
        runs.append_asins(claim, ["B0BANDAAA4"], band_id=b.band_id + 99999)
    assert run_row(runs._app, "US", claim.run_id).pages_fetched == 2


def test_append_asins_without_band_is_unchanged_and_never_touches_bands(runs, claim):
    runs.init_bands(claim, [(0, None)])
    band = runs.load_bands(claim)[0]
    runs.advance_band(claim, band.band_id, "ACTIVE")
    assert runs.append_asins(claim, ["B0NOBAND01", "B0NOBAND02"], pages=1) == 2
    b = runs.load_bands(claim)[0]
    assert (b.pages_fetched, b.asins_new) == (0, 0)


def test_finish_completeness_override_and_default(runs, registry, app_role_factory):
    a = runs.claim("US", new_source(registry).source_id)
    runs.append_asins(a, ["B0COMPL001"])
    runs.finish(a, "SUCCESS_NONEMPTY")
    assert run_row(app_role_factory, "US", a.run_id).completeness == "COMPLETE"  # 省略時は従来どおり
    b = runs.claim("US", new_source(registry).source_id)
    runs.append_asins(b, ["B0COMPL002"])
    with pytest.raises(ValueError, match="completeness"):
        runs.finish(b, "SUCCESS_NONEMPTY", completeness="WHATEVER")
    assert run_row(app_role_factory, "US", b.run_id).completeness is None  # 不正値では何も確定しない
    runs.finish(b, "SUCCESS_NONEMPTY", completeness="TRUNCATED")
    r = run_row(app_role_factory, "US", b.run_id)
    assert (r.status, r.outcome, r.completeness) == ("COMPLETED", "SUCCESS_NONEMPTY", "TRUNCATED")
