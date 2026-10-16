"""ビジネスレポート取込の結合テスト（実PostgreSQL、使い捨てtest-db）。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from origin_selection.report_import import ReportImportService, evaluate_blockers
from origin_selection.report_parser import ReportFormatError
from tests.integration.report_helpers import fixture_bytes, place, unique_period

pytestmark = pytest.mark.integration

_US_HEADER = (
    "（子）ASIN,セッション数 - 合計,セッション率 - 合計,おすすめ出品（おすすめ商品）の獲得率,"
    "注文された商品点数,ユニットセッション率,注文商品の売上額,注文品目総数"
)


@pytest.fixture()
def service(app_role_factory, kpi_report_factory) -> ReportImportService:
    return ReportImportService(app_role_factory, kpi_report_factory, actor="tester")


def _candidates(factory, mp, import_id):
    with factory.get_session(mp) as conn, conn.begin():
        return conn.execute(
            text(
                "SELECT rank, tier, asin, units_sum, sessions_sum, buybox_rate_max_pct, source_row_count, "
                "source_row_numbers FROM v_business_report_candidate WHERE import_id = :id ORDER BY rank"
            ),
            {"id": import_id},
        ).all()


def test_import_us_fixture_is_staged_with_traceability(tmp_path, service, app_role_factory):
    res, _ = place(tmp_path, "US", "any-name.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    out = service.import_file(res, ps, pe)
    r = out.report
    assert out.created and r.status == "STAGED"
    assert (r.marketplace, r.business_direction, r.market_basis) == ("US", "EXPORT", "FOLDER")
    assert (r.period_start, r.period_end, r.period_basis) == (ps, pe, "USER_SPECIFIED")
    assert (r.total_rows, r.loaded_rows, r.error_rows) == (8, 8, 0)
    assert r.encoding_detected in ("utf-8-sig",)
    assert len(r.file_sha256) == 64
    with app_role_factory.get_session("US") as conn, conn.begin():
        n = conn.execute(text("SELECT count(*) FROM business_report_row WHERE import_id=:i"), {"i": r.import_id}).scalar_one()
        ev = conn.execute(
            text("SELECT event_type, actor FROM business_report_import_event WHERE import_id=:i"), {"i": r.import_id}
        ).all()
        hist = conn.execute(
            text("SELECT import_type, status FROM import_run_history WHERE import_run_id=:r"), {"r": r.import_run_id}
        ).one()
    assert n == 8
    assert [(e.event_type, e.actor) for e in ev] == [("IMPORTED", "tester")]
    assert (hist.import_type, hist.status) == ("BUSINESS_REPORT", "COMPLETED")


def test_aggregation_and_ranking(tmp_path, service, app_role_factory):
    res, _ = place(tmp_path, "US", "r.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    rows = _candidates(app_role_factory, "US", r.import_id)

    assert [c.asin for c in rows] == [
        "B0TESTUS01", "B0TESTUS02", "B0TESTUS03",  # 注文点数の降順（9, 2, 1）
        "409199055X", "B0TESTUS05",  # 注文0・セッション7同点 → 最大獲得率(100 > 20)
        "0123456789",  # セッション5
        "B0TESTUS04",  # セッション合算3
    ]
    assert [c.rank for c in rows] == list(range(1, 8))
    assert [c.tier for c in rows] == [1, 1, 1, 2, 2, 2, 2]  # 注文0点も残る

    multi = next(c for c in rows if c.asin == "B0TESTUS04")  # 同一子ASINの2行
    assert (multi.units_sum, multi.sessions_sum, multi.buybox_rate_max_pct) == (0, 3, 50)
    assert multi.source_row_count == 2 and list(multi.source_row_numbers) == [4, 5]
    assert next(c for c in rows if c.asin == "B0TESTUS03").sessions_sum == 1430  # カンマ除去
    # 先頭0・末尾Xのpure文字列ASINが保持される
    assert {"0123456789", "409199055X"} <= {c.asin for c in rows}
    top = rows[0]
    assert top.units_sum == 9  # 注文品目総数(1)ではなく注文された商品点数(9)


def test_ranking_ties_are_deterministic_by_asin(tmp_path, service, app_role_factory):
    body = "\n".join(
        [
            _US_HEADER,
            "B0ZZZZZZ02,5,0%,10.00%,1,0%,$0,1",
            "B0AAAAAA01,5,0%,10.00%,1,0%,$0,1",
        ]
    )
    res, _ = place(tmp_path, "US", "tie.csv", body.encode("utf-8"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    assert [c.asin for c in _candidates(app_role_factory, "US", r.import_id)] == ["B0AAAAAA01", "B0ZZZZZZ02"]


def test_jp_fixture_direction_title_and_leading_zero(tmp_path, service, app_role_factory):
    res, _ = place(tmp_path, "JP", "jp.csv", fixture_bytes("synthetic_jp.csv"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    assert (r.marketplace, r.business_direction) == ("JP", "IMPORT")
    with app_role_factory.get_session("JP") as conn, conn.begin():
        titles = dict(conn.execute(text("SELECT asin, title FROM v_business_report_asin WHERE import_id=:i"), {"i": r.import_id}).all())
    assert titles["B0TESTJP01"] == "テスト商品, 2個セット" and titles["B0TESTJP02"] is None
    assert "0987654321" in titles
    rows = _candidates(app_role_factory, "JP", r.import_id)
    assert [c.asin for c in rows][:2] == ["B0TESTJP01", "B0TESTJP02"]  # 注文1点同士はセッション降順


def test_reimport_same_file_same_market_period_is_idempotent(tmp_path, service, app_role_factory):
    res, _ = place(tmp_path, "US", "r.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    first = service.import_file(res, ps, pe)
    second = service.import_file(res, ps, pe)
    assert first.created and not second.created
    assert second.report.import_id == first.report.import_id
    with app_role_factory.get_session("US") as conn, conn.begin():
        imports = conn.execute(text("SELECT count(*) FROM business_report_import WHERE period_start=:p"), {"p": ps}).scalar_one()
        rows = conn.execute(text("SELECT count(*) FROM business_report_row WHERE import_id=:i"), {"i": first.report.import_id}).scalar_one()
        runs = conn.execute(
            text("SELECT count(*) FROM import_run_history WHERE source_file_name='r.csv' AND import_type='BUSINESS_REPORT' AND import_run_id=:r"),
            {"r": first.report.import_run_id},
        ).scalar_one()
    assert (imports, rows, runs) == (1, 8, 1)


def test_same_file_other_period_is_a_new_import_with_warning(tmp_path, service):
    res, _ = place(tmp_path, "US", "r.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    first = service.import_file(res, ps, pe)
    ps2, pe2 = unique_period()
    second = service.import_file(res, ps2, pe2)
    assert second.created and second.report.import_id != first.report.import_id
    assert any("別期間" in w and str(first.report.import_id) in w for w in second.warnings)


def test_same_file_other_market_is_not_skipped_and_warns_via_read_only_role(tmp_path, service, app_role_factory):
    content = fixture_bytes("synthetic_us.csv")
    res_us, _ = place(tmp_path, "US", "r.csv", content)
    ps, pe = unique_period()
    us = service.import_file(res_us, ps, pe)
    res_jp, _ = place(tmp_path, "JP", "r.csv", content)  # 同一バイト列を別市場のフォルダへ
    jp = service.import_file(res_jp, ps, pe)  # 同じ期間でも市場が違えば別の取込
    assert jp.created and jp.report.marketplace == "JP" and jp.report.import_id != us.report.import_id
    assert any("別市場(US)" in w and str(us.report.import_id) in w for w in jp.warnings)
    # 警告のために市場別のapp_roleのRLSを回避していない: JPセッションからUSの行は見えないまま
    with app_role_factory.get_session("JP") as conn, conn.begin():
        ids = [r[0] for r in conn.execute(text("SELECT import_id FROM business_report_import")).all()]
    assert us.report.import_id not in ids and jp.report.import_id in ids


def test_hash_warning_is_skipped_without_read_only_factory(tmp_path, app_role_factory):
    svc = ReportImportService(app_role_factory, None, actor="tester")
    res, _ = place(tmp_path, "US", "r.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    out = svc.import_file(res, ps, pe)
    assert any("省略" in w for w in out.warnings)


def test_us_folder_file_with_jp_shape_is_still_us(tmp_path, service):
    """タイトル列・円表記を持つJP形式でも、USフォルダならUSとして取り込まれる（内容で判定しない）。"""
    res, _ = place(tmp_path, "US", "JP-BusinessReport-x.csv", fixture_bytes("synthetic_jp.csv"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    assert (r.marketplace, r.business_direction) == ("US", "EXPORT")


def test_cp932_file_imports_with_same_result(tmp_path, service, app_role_factory):
    text_utf8 = fixture_bytes("synthetic_us.csv").decode("utf-8")
    res, _ = place(tmp_path, "US", "cp.csv", text_utf8.encode("cp932"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    assert r.encoding_detected == "cp932"
    assert [c.asin for c in _candidates(app_role_factory, "US", r.import_id)][0] == "B0TESTUS01"


def test_row_errors_are_isolated_logged_and_block_activation(tmp_path, service, app_role_factory):
    body = "\n".join(
        [
            _US_HEADER,
            "B0TESTUS01,16,0.02%,3.45%,9,56.25%,$1.00,1",
            "BAD,16,0.02%,3.45%,9,56.25%,$1.00,1",
            "B0TESTUS02,x,0.02%,3.45%,9,56.25%,$1.00,1",
            "B0TESTUS03,2,0.02%,1.00%,3,56.25%,$1.00,1",
        ]
    )
    res, _ = place(tmp_path, "US", "err.csv", body.encode("utf-8"))
    ps, pe = unique_period()
    r = service.import_file(res, ps, pe).report
    assert (r.total_rows, r.loaded_rows, r.error_rows) == (4, 2, 2)  # 他の行の取込は継続
    assert r.status == "STAGED"
    errors = service.get_errors(r.import_id, "US")
    assert [(e[0], e[1]) for e in errors] == [(2, "IM-REPORT-01"), (3, "IM-REPORT-01")]
    assert evaluate_blockers(r, None, "first")  # 有効化できない
    assert [c.asin for c in _candidates(app_role_factory, "US", r.import_id)] == ["B0TESTUS01", "B0TESTUS03"]

    # DBのCHECKでも有効化できない（アプリ層をすり抜けた直接UPDATEでも拒否される）
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="ck_bri_active_no_errors"):
            with conn.begin():
                conn.execute(
                    text("UPDATE business_report_import SET status='ACTIVE', activated_at=now() WHERE import_id=:i"),
                    {"i": r.import_id},
                )


def test_fatal_format_error_creates_no_import_and_marks_run_failed(tmp_path, service, app_role_factory):
    res, _ = place(tmp_path, "US", "fatal.csv", "（子）ASIN,セッション数 - 合計\nB0TESTUS01,1\n".encode("utf-8"))
    ps, pe = unique_period()
    with pytest.raises(ReportFormatError):
        service.import_file(res, ps, pe)
    with app_role_factory.get_session("US") as conn, conn.begin():
        n = conn.execute(text("SELECT count(*) FROM business_report_import WHERE period_start=:p"), {"p": ps}).scalar_one()
        failed = conn.execute(
            text(
                "SELECT count(*) FROM import_run_history WHERE import_type='BUSINESS_REPORT' "
                "AND status='FAILED' AND source_file_name='fatal.csv'"
            )
        ).scalar_one()
    assert n == 0 and failed >= 1


def test_period_end_before_start_is_rejected(tmp_path, service):
    res, _ = place(tmp_path, "US", "r.csv", fixture_bytes("synthetic_us.csv"))
    ps, pe = unique_period()
    with pytest.raises(ValueError):
        service.import_file(res, pe, ps)
