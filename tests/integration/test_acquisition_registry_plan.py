"""出典の登録・発見経路・方向別planの結合テスト（実PostgreSQL、使い捨てtest-db）。"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from acquisition import normalize
from acquisition.lease import NotEligible
from acquisition.normalize import KeyCollisionError
from acquisition.plan import PlanError, build_plan
from acquisition.registry import OriginError, SourceRegistry
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import insert_old_success, make_active_report, make_runs, new_source, unique_key

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


@pytest.fixture()
def reports(app_role_factory):
    return ReportImportService(app_role_factory, None)


def test_same_source_is_one_row_regardless_of_notation_and_keeps_display(registry, app_role_factory):
    key = unique_key("japanese woodworking ")
    a = registry.register_source("US", "KEYWORD", key.replace(" ", "　"), "Office-Products")
    b = registry.register_source("US", "KEYWORD", "  " + key.upper() + "  ", "office-products")
    assert (a.source_id, a.created, b.created) == (b.source_id, True, False)
    with app_role_factory.get_session("US") as conn, conn.begin():
        row = conn.execute(text("SELECT display_primary, key_primary FROM acquisition_source WHERE source_id = :id"), {"id": a.source_id}).one()
    assert "　" in row.display_primary and "　" not in row.key_primary  # 元の表記は保持、キーは正規化


def test_different_market_type_qualifier_are_different_sources(registry):
    base = unique_key("kw")
    ids = {
        registry.register_source("US", "KEYWORD", base).source_id,
        registry.register_source("JP", "KEYWORD", base).source_id,
        registry.register_source("US", "BRAND_NAME", base).source_id,
        registry.register_source("US", "KEYWORD", base, "toys").source_id,
        registry.register_source("US", "KEYWORD", base, "office").source_id,
    }
    assert len(ids) == 5


def test_delimiter_characters_do_not_make_different_keys_collide(registry):
    a = registry.register_source("US", "KEYWORD", "a|b", "c" + unique_key())
    b = registry.register_source("US", "KEYWORD", "a", "b|c" + unique_key())
    assert a.source_id != b.source_id


def test_very_long_keyword_is_stored_and_found_again(registry, app_role_factory):
    long = unique_key("L") + " " + "x" * 20000
    a = registry.register_source("US", "KEYWORD", long)
    assert registry.register_source("US", "KEYWORD", long).source_id == a.source_id
    with app_role_factory.get_session("US") as conn, conn.begin():
        n = conn.execute(text("SELECT length(key_primary) FROM acquisition_source WHERE source_id = :id"), {"id": a.source_id}).scalar_one()
    assert n > 20000


def test_hash_collision_is_detected_by_comparing_the_original_values(registry, monkeypatch):
    first = registry.register_source("US", "KEYWORD", unique_key("c1"))
    other = unique_key("c2")
    real = normalize.key_hash
    import acquisition.registry as reg

    forced = real("KEYWORD", first.key_primary, "")
    monkeypatch.setattr(reg, "key_hash", lambda t, p, q: forced if p == other.lower() else real(t, p, q))
    with pytest.raises(KeyCollisionError):
        registry.register_source("US", "KEYWORD", other)


def test_qualifier_cannot_be_null_and_hash_format_is_enforced(app_role_factory):
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(IntegrityError, match="null value"):
            with conn.begin():
                conn.execute(text("INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_qualifier, key_hash, display_primary) "
                                  "VALUES ('US','KEYWORD','x',NULL, :h, 'x')"), {"h": "0" * 64})
        with pytest.raises(IntegrityError, match="ck_as_key_hash"):
            with conn.begin():
                conn.execute(text("INSERT INTO acquisition_source (marketplace, source_type, key_primary, key_hash, display_primary) "
                                  "VALUES ('US','KEYWORD','x','not-a-hash','x')"))


def test_same_source_found_from_two_origins_and_two_directions_is_one_source_with_all_paths(tmp_path, registry, reports, runs, app_role_factory):
    us_import = make_active_report(app_role_factory, tmp_path, "US", [("B0USORIGN1", 100, 40.0, 9), ("B0USORIGN2", 50, 10.0, 2)])
    jp_import = make_active_report(app_role_factory, tmp_path, "JP", [("B0JPORIGN1", 70, 20.0, 1)])
    key = unique_key("AJPSHARED")
    # 同じJPセラー（取得先市場=JP）を、輸出(US起点)と輸入(JP起点)の両方の経路から発見
    r1 = registry.register_path("JP", "SELLER", key, origin_import_id=us_import, origin_asin="B0USORIGN1")
    r2 = registry.register_path("JP", "SELLER", key, origin_import_id=us_import, origin_asin="B0USORIGN2")
    r3 = registry.register_path("JP", "SELLER", key, origin_import_id=jp_import, origin_asin="B0JPORIGN1")
    again = registry.register_path("JP", "SELLER", key, origin_import_id=us_import, origin_asin="B0USORIGN1")
    assert len({r1.source.source_id, r2.source.source_id, r3.source.source_id}) == 1
    assert (r1.path_created, r2.path_created, r3.path_created, again.path_created) == (True, True, True, False)
    with app_role_factory.get_session("JP") as conn, conn.begin():
        paths = conn.execute(text("SELECT direction, origin_marketplace, origin_import_id, origin_asin, origin_units, origin_sessions, "
                                  "origin_buybox_max_pct FROM acquisition_path WHERE source_id = :id ORDER BY path_id"),
                             {"id": r1.source.source_id}).all()
    assert [(p.direction, p.origin_marketplace, p.origin_asin, p.origin_units, p.origin_sessions, float(p.origin_buybox_max_pct)) for p in paths] == [
        ("EXPORT", "US", "B0USORIGN1", 9, 100, 40.0), ("EXPORT", "US", "B0USORIGN2", 2, 50, 10.0),
        ("IMPORT", "JP", "B0JPORIGN1", 1, 70, 20.0)]
    # 取得は1回: 1つのclaimが成功すれば、もう一方の方向からも取得間隔内
    claim = runs.claim("JP", r1.source.source_id)
    runs.finish(claim, "SUCCESS_NONEMPTY")
    with pytest.raises(NotEligible):
        runs.claim("JP", r3.source.source_id)


def test_origin_must_be_an_active_error_free_report_with_the_asin(tmp_path, registry, app_role_factory, reports):
    ok = make_active_report(app_role_factory, tmp_path, "US", [("B0ORIGINOK", 10, 1.0, 1)])
    with pytest.raises(OriginError, match="存在しません"):
        registry.register_path("US", "SELLER", unique_key(), origin_import_id=999999999, origin_asin="B0ORIGINOK")
    with pytest.raises(OriginError, match="ASIN"):
        registry.register_path("US", "SELLER", unique_key(), origin_import_id=ok, origin_asin="B0NOSUCHAS")
    with pytest.raises(OriginError, match="両方"):
        registry.register_path("US", "SELLER", unique_key(), origin_import_id=ok)
    # STAGED（有効化していない）レポートは使えない
    from tests.integration.report_helpers import place, unique_period

    res, _ = place(tmp_path, "US", "staged.csv", "（子）ASIN,セッション数 - 合計,おすすめ出品（おすすめ商品）の獲得率,注文された商品点数\nB0STAGEDAA,1,1%,1\n".encode())
    staged = reports.import_file(res, *unique_period()).report
    with pytest.raises(OriginError, match="ACTIVE"):
        registry.register_path("US", "SELLER", unique_key(), origin_import_id=staged.import_id, origin_asin="B0STAGEDAA")
    # 行エラーのあるレポートは、有効化できないので使えない
    bad, _ = place(tmp_path, "US", "bad.csv", "（子）ASIN,セッション数 - 合計,おすすめ出品（おすすめ商品）の獲得率,注文された商品点数\nB0GOODROWX,1,1%,1\nBAD,1,1%,1\n".encode())
    badinfo = reports.import_file(bad, *unique_period()).report
    with pytest.raises(OriginError):
        registry.register_path("US", "SELLER", unique_key(), origin_import_id=badinfo.import_id, origin_asin="B0GOODROWX")


def test_plan_requires_explicit_valid_reports_and_never_mixes_periods(tmp_path, registry, reports, app_role_factory):
    us1 = make_active_report(app_role_factory, tmp_path, "US", [("B0PLANUS01", 10, 5.0, 3)])
    us2 = make_active_report(app_role_factory, tmp_path, "US", [("B0PLANUS02", 10, 5.0, 500)], name="r2.csv")  # 別期間
    jp = make_active_report(app_role_factory, tmp_path, "JP", [("B0PLANJP01", 10, 5.0, 1)])
    with pytest.raises(PlanError, match="明示"):
        build_plan(app_role_factory, reports, "JP", [])
    with pytest.raises(PlanError, match="同じ市場"):
        build_plan(app_role_factory, reports, "JP", [us1, us2])
    with pytest.raises(PlanError, match="存在しません"):
        build_plan(app_role_factory, reports, "JP", [999999999])
    key_old, key_new = unique_key("AOLD"), unique_key("ANEW")
    registry.register_path("JP", "SELLER", key_old, origin_import_id=us2, origin_asin="B0PLANUS02")  # 注文500点の別期間
    registry.register_path("JP", "SELLER", key_new, origin_import_id=us1, origin_asin="B0PLANUS01")
    plan = build_plan(app_role_factory, reports, "JP", [us1, jp], limit=None)
    seen = {e.facts.key_primary for s in plan.sections.values() for e in s}
    assert key_new.upper() in seen and key_old.upper() not in seen.difference({e.facts.key_primary for e in plan.sections.get("NO_ORIGIN", [])})
    # 指定していない過去期間(us2)由来の経路は、順位に使われない（起点なし扱い）
    entry_old = [e for e in plan.sections.get("NO_ORIGIN", []) if e.facts.key_primary == key_old.upper()]
    assert entry_old and entry_old[0].rep is None


def test_representative_origin_is_not_summed_and_sections_are_by_direction(tmp_path, registry, reports, app_role_factory, runs):
    us = make_active_report(app_role_factory, tmp_path, "US", [("B0REPUS001", 100, 10.0, 9), ("B0REPUS002", 100, 10.0, 2), ("B0REPUS003", 5, 90.0, 10)])
    jp = make_active_report(app_role_factory, tmp_path, "JP", [("B0REPJP001", 80, 50.0, 1)])
    x, y, z = unique_key("AXX"), unique_key("AYY"), unique_key("AZZ")
    registry.register_path("JP", "SELLER", x, origin_import_id=us, origin_asin="B0REPUS001")  # 9点
    registry.register_path("JP", "SELLER", x, origin_import_id=us, origin_asin="B0REPUS002")  # +2点（合算すると11）
    registry.register_path("JP", "SELLER", y, origin_import_id=us, origin_asin="B0REPUS003")  # 10点
    registry.register_path("JP", "SELLER", x, origin_import_id=jp, origin_asin="B0REPJP001")  # 同じ出典を輸入側でも発見
    registry.register_path("JP", "SELLER", z, origin_import_id=jp, origin_asin="B0REPJP001")
    plan = build_plan(app_role_factory, reports, "JP", [us, jp])
    export = [e for e in plan.sections["EXPORT"] if e.facts.key_primary in {x.upper(), y.upper(), z.upper()}]
    assert [e.facts.key_primary for e in export] == [y.upper(), x.upper()]  # 代表起点10点 > 9点（9+2=11にはならない）
    assert [e.rep.units for e in export] == [10, 9]
    imp = [e for e in plan.sections["IMPORT"] if e.facts.key_primary in {x.upper(), z.upper()}]
    assert {e.facts.key_primary for e in imp} == {x.upper(), z.upper()}
    shared = next(e for e in imp if e.facts.key_primary == x.upper())
    assert shared.also_in == ["EXPORT"]  # 両方向から発見された出典は共有取得と表示
    merged = build_plan(app_role_factory, reports, "JP", [us, jp], group_by_direction=False)
    assert "ALL" in merged.sections and "EXPORT" not in merged.sections


def test_plan_orders_unfetched_before_refetch_and_lists_blocked_with_reasons(tmp_path, registry, reports, runs, app_role_factory):
    us = make_active_report(app_role_factory, tmp_path, "US", [("B0ORDUS001", 1, 1.0, 100), ("B0ORDUS002", 1, 1.0, 1)])
    strong, weak, fetched, held, excluded, running = (unique_key(p) for p in ("ASTR", "AWEA", "AFET", "AHLD", "AEXC", "ARUN"))
    for k, asin in ((strong, "B0ORDUS001"), (weak, "B0ORDUS002"), (fetched, "B0ORDUS001"), (held, "B0ORDUS001"),
                    (excluded, "B0ORDUS001"), (running, "B0ORDUS001")):
        registry.register_path("US", "SELLER", k, origin_import_id=us, origin_asin=asin)
    ids = {k: registry.register_source("US", "SELLER", k).source_id for k in (strong, weak, fetched, held, excluded, running)}
    insert_old_success(app_role_factory, "US", ids[fetched], days_ago=45)  # 45日前に取得済み → 再取得可能
    registry.set_status("US", ids[held], "ON_HOLD", "テスト保留")
    registry.set_status("US", ids[excluded], "EXCLUDED", "テスト除外")
    runs.claim("US", ids[running])
    plan = build_plan(app_role_factory, reports, "US", [us])
    mine = [e for e in plan.sections["EXPORT"] if e.facts.source_id in ids.values()]
    # 実行可能な未取得(strong, weak)が先、再取得(fetched)は後。各群の中は代表起点の注文点数順
    assert [(e.facts.source_id, e.group) for e in mine] == [(ids[strong], "NEVER"), (ids[weak], "NEVER"), (ids[fetched], "REFETCH")]
    blocked = {f.source_id: d.state for f, d in plan.blocked if f.source_id in ids.values()}
    assert blocked == {ids[held]: "ON_HOLD", ids[excluded]: "EXCLUDED", ids[running]: "RUNNING"}
    for s in plan.sections.values():
        assert all(e.facts.source_id not in blocked for e in s)  # 実行不可は順位に載らない
