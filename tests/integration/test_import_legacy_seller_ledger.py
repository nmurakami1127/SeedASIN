"""scripts.import_legacy_seller_ledger.import_legacy_ledger()の結合テスト:
実PostgreSQL 16に対してknown_seller_master/tool_execution_link/storefront_blocked_seller
への振り分けを検証する。Excel解析自体はtests/unit/test_import_legacy_seller_ledger.py参照
（ここではLegacyRowを直接組み立て、DB書き込みロジックのみを対象にする）。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from scripts.import_legacy_seller_ledger import LegacyRow, import_legacy_ledger
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

pytestmark = pytest.mark.integration


def _make_tool_file(directory, tool_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{tool_id}_someone@example.com_2026-01-01 00_00_00.csv").write_text("x")


def test_normal_row_creates_known_seller_and_tool_execution_link(
    tmp_path, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_dir = tmp_path / "primary_check"
    us_dir = tmp_path / "ex_primary_check"
    _make_tool_file(jp_dir, tool_id)

    rows = [
        LegacyRow(
            row_date=date(2026, 2, 10),
            source_asin="B000RTVJ8G",
            seller_id=seller_id,
            is_brand_row=False,
            tool_id=tool_id,
            status="取得済み",
        )
    ]

    summary = import_legacy_ledger(app_role_factory, rows, jp_dir, us_dir)

    assert summary.new_known_sellers == 1
    assert summary.tool_execution_links_recorded == 1
    assert summary.files_not_found == 0

    with app_role_factory.get_session("JP") as conn:
        seller_row = conn.execute(
            text(
                "SELECT first_known_at::date AS d, known_before_seedasin, known_at_reliable, "
                "source_asin, last_seen_at::date AS last_d "
                "FROM known_seller_master WHERE marketplace='JP' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert seller_row.d == date(2026, 2, 10)
        assert seller_row.known_before_seedasin is True
        assert seller_row.known_at_reliable is True
        assert seller_row.source_asin == "B000RTVJ8G"
        assert seller_row.last_d == date(2026, 2, 10)

        link_row = conn.execute(
            text(
                "SELECT seller_id, source, requested_at::date AS d FROM tool_execution_link "
                "WHERE marketplace='JP' AND tool_run_id=:rid"
            ),
            {"rid": tool_id},
        ).one()
        assert link_row.seller_id == seller_id
        assert link_row.source == "LEGACY_IMPORT"
        assert link_row.d == date(2026, 2, 10)


def test_multiple_occurrences_use_oldest_first_known_at_and_newest_last_seen_at(
    tmp_path, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id_1 = f"{uuid.uuid4().int % 10_000_000:07d}"
    tool_id_2 = f"{(uuid.uuid4().int + 1) % 10_000_000:07d}"
    us_dir = tmp_path / "ex_primary_check"
    _make_tool_file(us_dir, tool_id_1)
    _make_tool_file(us_dir, tool_id_2)

    rows = [
        LegacyRow(date(2026, 5, 10), "B0AAAAAAAA", seller_id, False, tool_id_2, "取得済み"),
        LegacyRow(date(2026, 5, 1), "B0BBBBBBBB", seller_id, False, tool_id_1, "取得済み"),  # より古い
    ]

    summary = import_legacy_ledger(app_role_factory, rows, tmp_path / "primary_check", us_dir)

    assert summary.new_known_sellers == 1
    assert summary.tool_execution_links_recorded == 2

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text(
                "SELECT first_known_at::date AS d, last_seen_at::date AS last_d, source_asin "
                "FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"
            ),
            {"sid": seller_id},
        ).one()
        assert row.d == date(2026, 5, 1)  # 最古
        assert row.last_d == date(2026, 5, 10)  # 最新
        assert row.source_asin == "B0BBBBBBBB"  # 最古の行の起点ASIN


def test_existing_known_seller_is_not_overwritten(tmp_path, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_dir = tmp_path / "primary_check"
    _make_tool_file(jp_dir, tool_id)

    with app_role_factory.get_session("JP") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('JP', :sid, '2020-01-01', true, false, '2020-01-01')"
            ),
            {"sid": seller_id},
        )

    rows = [LegacyRow(date(2026, 2, 10), "B000RTVJ8G", seller_id, False, tool_id, "取得済み")]
    summary = import_legacy_ledger(app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check")

    assert summary.new_known_sellers == 0
    assert summary.skipped_count >= 1

    with app_role_factory.get_session("JP") as conn:
        row = conn.execute(
            text("SELECT first_known_at::date AS d FROM known_seller_master WHERE marketplace='JP' AND seller_id=:sid"),
            {"sid": seller_id},
        ).one()
        assert row.d == date(2020, 1, 1)  # 上書きされていない


def test_existing_tool_execution_link_is_skipped(tmp_path, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_dir = tmp_path / "primary_check"
    _make_tool_file(jp_dir, tool_id)

    rows = [LegacyRow(date(2026, 2, 10), "B000RTVJ8G", seller_id, False, tool_id, "取得済み")]
    first = import_legacy_ledger(app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check")
    assert first.tool_execution_links_recorded == 1

    second = import_legacy_ledger(app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check")
    assert second.tool_execution_links_recorded == 0
    assert second.skipped_count >= 1


def test_forbidden_row_is_recorded_to_storefront_blocked_seller_not_known_seller(
    tmp_path, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    rows = [LegacyRow(date(2026, 5, 8), "B005Q75XJU", seller_id, False, "*", "禁止セラー")]

    summary = import_legacy_ledger(
        app_role_factory, rows, tmp_path / "primary_check", tmp_path / "ex_primary_check"
    )

    assert summary.storefront_blocked_recorded == 1
    assert summary.new_known_sellers == 0
    assert summary.tool_execution_links_recorded == 0

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.first_noted_at == date(2026, 5, 8)


def test_forbidden_row_duplicate_seller_uses_oldest_date(tmp_path, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    rows = [
        LegacyRow(date(2026, 6, 1), "B0AAAAAAAA", seller_id, False, "*", "禁止セラー"),
        LegacyRow(date(2026, 5, 1), "B0BBBBBBBB", seller_id, False, "*", "禁止セラー"),  # より古い
    ]

    import_legacy_ledger(app_role_factory, rows, tmp_path / "primary_check", tmp_path / "ex_primary_check")

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info.first_noted_at == date(2026, 5, 1)


def test_row_with_unfindable_tool_id_is_reported_as_file_not_found(tmp_path, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    rows = [LegacyRow(date(2026, 5, 10), "B005Q75XJU", seller_id, False, "9999999", "取得済み")]

    summary = import_legacy_ledger(
        app_role_factory, rows, tmp_path / "primary_check", tmp_path / "ex_primary_check"
    )

    assert summary.files_not_found == 1
    assert summary.new_known_sellers == 0
    assert summary.tool_execution_links_recorded == 0


def test_deferred_row_with_dash_tool_id_is_file_not_found_without_false_match(
    tmp_path, app_role_factory: SessionFactory
):
    """'-'のような実在しないtool_run_idで、フォルダ内のファイル名にたまたま含まれる
    文字と誤検出しないこと（安全側のis_plausible_tool_idガード）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir(parents=True)
    (jp_dir / "some-file_with-dashes_2026-01-01.csv").write_text("x")  # '-'を含むファイル名

    rows = [LegacyRow(date(2026, 5, 10), "B005Q75XJU", seller_id, False, "-", "見送る")]
    summary = import_legacy_ledger(app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check")

    assert summary.files_not_found == 1


def test_brand_row_is_counted_in_brand_rows_imported(tmp_path, app_role_factory: SessionFactory):
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_dir = tmp_path / "primary_check"
    _make_tool_file(jp_dir, tool_id)

    rows = [LegacyRow(date(2026, 2, 10), "B0CM14LLLK2", "ELDIYME", True, tool_id, "取得済み")]
    summary = import_legacy_ledger(app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check")

    assert summary.brand_rows_imported == 1
    assert summary.tool_execution_links_recorded == 1


def test_dry_run_does_not_write_to_database(tmp_path, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_id = f"{uuid.uuid4().int % 10_000_000:07d}"
    jp_dir = tmp_path / "primary_check"
    _make_tool_file(jp_dir, tool_id)

    rows = [LegacyRow(date(2026, 2, 10), "B000RTVJ8G", seller_id, False, tool_id, "取得済み")]
    summary = import_legacy_ledger(
        app_role_factory, rows, jp_dir, tmp_path / "ex_primary_check", dry_run=True
    )

    assert summary.new_known_sellers == 1  # 集計上は1件だが...
    with app_role_factory.get_session("JP") as conn:
        rows_in_db = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='JP' AND seller_id=:sid"),
            {"sid": seller_id},
        ).fetchall()
        assert rows_in_db == []  # ...実際には書き込まれていない
