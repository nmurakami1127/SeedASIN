"""scripts.import_legacy_tool_output.import_legacy_tool_output()の結合テスト:
実PostgreSQL 16に対してtool_output_asinへの一括取込を検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from scripts.import_legacy_tool_output import import_legacy_tool_output

pytestmark = pytest.mark.integration

_HEADER = "ASIN,取得日時"


def _write_tool_output_csv(directory, tool_id: str, rows: list[tuple[str, str]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tool_id}_someone@example.com_2026-01-01 00_00_00.csv"
    lines = [_HEADER] + [f"{asin},{acquired_at}" for asin, acquired_at in rows]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_imports_asins_from_a_real_file(tmp_path, app_role_factory: SessionFactory):
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
    jp_dir = tmp_path / "primary_check"
    _write_tool_output_csv(
        jp_dir, tool_id, [(asin1, "2026-02-10 10:00:00"), (asin2, "2026-02-10 10:05:00")]
    )

    links = [("JP", tool_id)]
    summary = import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")

    assert summary.asins_imported == 2
    assert summary.files_processed == 1
    assert summary.files_not_found == 0

    with app_role_factory.get_session("JP") as conn:
        rows = conn.execute(
            text("SELECT asin FROM tool_output_asin WHERE marketplace='JP' AND tool_run_id=:rid ORDER BY asin"),
            {"rid": tool_id},
        ).fetchall()
        assert sorted(r[0] for r in rows) == sorted([asin1, asin2])


def test_missing_file_is_reported_without_error(tmp_path, app_role_factory: SessionFactory):
    links = [("US", "9999999")]
    summary = import_legacy_tool_output(
        app_role_factory, links, tmp_path / "primary_check", tmp_path / "ex_primary_check"
    )

    assert summary.files_not_found == 1
    assert summary.files_processed == 0
    assert summary.asins_imported == 0


def test_implausible_tool_id_is_reported_as_not_found_without_filesystem_search(
    tmp_path, app_role_factory: SessionFactory
):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    (jp_dir / "some-file_with-dashes.csv").write_text("x")

    links = [("JP", "-")]
    summary = import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")

    assert summary.files_not_found == 1


def test_reimporting_the_same_file_is_idempotent(tmp_path, app_role_factory: SessionFactory):
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    jp_dir = tmp_path / "primary_check"
    _write_tool_output_csv(jp_dir, tool_id, [(asin, "2026-02-10 10:00:00")])

    links = [("JP", tool_id)]
    first = import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")
    second = import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")

    assert first.asins_imported == 1
    assert second.asins_imported == 0  # ON CONFLICT DO NOTHINGで重複なし

    with app_role_factory.get_session("JP") as conn:
        count = conn.execute(
            text("SELECT count(*) FROM tool_output_asin WHERE marketplace='JP' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_id, "asin": asin},
        ).scalar_one()
        assert count == 1


def test_row_with_bad_data_is_skipped_but_other_rows_in_same_file_still_import(
    tmp_path, app_role_factory: SessionFactory
):
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    good_asin = f"B{uuid.uuid4().hex[:9].upper()}"
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir(parents=True)
    path = jp_dir / f"{tool_id}_someone@example.com_2026-01-01 00_00_00.csv"
    # 1行目はASIN空欄（不正）、2行目は正常。
    path.write_text(f"{_HEADER}\n,2026-02-10 10:00:00\n{good_asin},2026-02-10 10:05:00\n", encoding="utf-8")

    links = [("JP", tool_id)]
    summary = import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")

    assert summary.parse_errors == 1
    assert summary.asins_imported == 1

    with app_role_factory.get_session("JP") as conn:
        row = conn.execute(
            text("SELECT asin FROM tool_output_asin WHERE marketplace='JP' AND tool_run_id=:rid"),
            {"rid": tool_id},
        ).one()
        assert row.asin == good_asin


def test_does_not_touch_known_seller_master_latest_checked_at(tmp_path, app_role_factory: SessionFactory):
    """import_file()と異なり、この移行スクリプトはlatest_product_count/latest_checked_at
    を更新しない（過去データを「たった今チェックした」ことにしないため、ユーザー確認済み）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    jp_dir = tmp_path / "primary_check"
    _write_tool_output_csv(jp_dir, tool_id, [(asin, "2026-02-10 10:00:00")])

    with app_role_factory.get_session("JP") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('JP', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) "
                "VALUES ('JP', :sid, :rid, 'LEGACY_IMPORT')"
            ),
            {"sid": seller_id, "rid": tool_id},
        )

    links = [("JP", tool_id)]
    import_legacy_tool_output(app_role_factory, links, jp_dir, tmp_path / "ex_primary_check")

    with app_role_factory.get_session("JP") as conn:
        row = conn.execute(
            text(
                "SELECT latest_product_count, latest_checked_at FROM known_seller_master "
                "WHERE marketplace='JP' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert row.latest_product_count is None
        assert row.latest_checked_at is None


def test_dry_run_does_not_write_to_database(tmp_path, app_role_factory: SessionFactory):
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    jp_dir = tmp_path / "primary_check"
    _write_tool_output_csv(jp_dir, tool_id, [(asin, "2026-02-10 10:00:00")])

    links = [("JP", tool_id)]
    summary = import_legacy_tool_output(
        app_role_factory, links, jp_dir, tmp_path / "ex_primary_check", dry_run=True
    )

    assert summary.files_processed == 1
    with app_role_factory.get_session("JP") as conn:
        rows = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='JP' AND tool_run_id=:rid"), {"rid": tool_id}
        ).fetchall()
        assert rows == []
