"""tool_intake.log_ingest.ingest_log()結合テスト: 実PostgreSQL 16に対して
register/link-tool-run/import-tool-outputのまとめ実行を検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from tool_intake.log_ingest import ingest_log

pytestmark = pytest.mark.integration

_TOOL_HEADER = "ASIN,取得日時"


def _tool_output_csv(tmp_path, filename: str, asin: str) -> None:
    (tmp_path / filename).write_text(f"{_TOOL_HEADER}\n{asin},2026-09-01 12:00:00\n", encoding="utf-8")


def _tr(tool_run_id: str, memo: str, country: str, status: str, csv_filename: str | None, data_type: str = "keyword") -> str:
    csv_text = csv_filename or "-"
    return f"""
    <tr data-seq="{tool_run_id}" data-country="{country}" data-type="{data_type}">
      <td class="log-status">{status}</td>
      <td class="log-csv">{csv_text}</td>
      <td class="log-memo"><textarea class="memo">{memo}</textarea></td>
    </tr>
    """


def _html(*trs: str) -> str:
    return "<table><tbody>" + "".join(trs) + "</tbody></table>"


@pytest.fixture()
def seller_id() -> str:
    return f"S{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _app_role_env(monkeypatch, superuser_url: str):
    from tests.integration.conftest import role_url

    monkeypatch.setenv("SEEDASIN_APP_DATABASE_URL", role_url(superuser_url, "app_role"))


def test_ingest_log_registers_links_and_imports(tmp_path, app_role_factory: SessionFactory, seller_id: str):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    csv_filename = f"{tool_run_id}_output.csv"
    _tool_output_csv(tmp_path, csv_filename, asin)

    html = _html(_tr(tool_run_id, f"輸出_{seller_id}", "jp", "取得完了", csv_filename))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    summary = ingest_log(SessionFactory.for_app_role(), html_path, tool_output_dir=tmp_path)

    assert summary.total_rows == 1
    assert summary.parse_errors == 0
    assert summary.new_sellers == 1
    assert summary.imports_attempted == 1
    assert summary.imports_succeeded == 1
    assert summary.files_not_found == 0

    with app_role_factory.get_session("US") as conn:
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(seller_row) == 1

        link_row = conn.execute(
            text("SELECT seller_id FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).one()
        assert link_row.seller_id == seller_id

        asin_row = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_run_id, "asin": asin},
        ).fetchall()
        assert len(asin_row) == 1


def test_ingest_log_skips_import_when_status_not_completed(tmp_path, seller_id: str):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, seller_id, "jp", "待機中", None))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    summary = ingest_log(SessionFactory.for_app_role(), html_path, tool_output_dir=tmp_path)

    assert summary.new_sellers == 1
    assert summary.imports_attempted == 0


def test_ingest_log_forbidden_seller_registers_but_does_not_import(tmp_path, seller_id: str):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, f"輸入_{seller_id}", "us", "禁止セラー", None))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    summary = ingest_log(SessionFactory.for_app_role(), html_path, tool_output_dir=tmp_path)

    assert summary.forbidden_sellers == 1
    assert summary.new_sellers == 1  # register/linkは禁止セラーでも実行される
    assert summary.imports_attempted == 0


def test_ingest_log_forbidden_seller_is_recorded_to_storefront_blocked_seller(tmp_path, seller_id: str):
    from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, f"輸入_{seller_id}", "us", "禁止セラー", None))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    factory = SessionFactory.for_app_role()
    summary = ingest_log(factory, html_path, tool_output_dir=tmp_path)

    assert summary.storefront_blocked_recorded == 1
    assert summary.storefront_blocked_already_recorded == 0

    with factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"


def test_ingest_log_forbidden_seller_already_blocked_is_skipped_not_duplicated(tmp_path, seller_id: str):
    """同じログファイルを2回取込しても、既にリンク済み（DuplicateToolRunError）とは独立して
    storefront_blocked_sellerの記録は重複しないこと（2回目は既存件数として数えられる）。"""
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, f"輸入_{seller_id}", "us", "禁止セラー", None))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    factory = SessionFactory.for_app_role()
    first = ingest_log(factory, html_path, tool_output_dir=tmp_path)
    assert first.storefront_blocked_recorded == 1

    second = ingest_log(factory, html_path, tool_output_dir=tmp_path)
    assert second.storefront_blocked_recorded == 0
    assert second.storefront_blocked_already_recorded == 1
    assert second.already_linked == 1  # link-tool-run側は既存の重複防止ロジックのまま


def test_ingest_log_file_not_found_is_recorded_not_raised(tmp_path, seller_id: str):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, f"輸出_{seller_id}", "jp", "取得完了", "does_not_exist.csv"))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    summary = ingest_log(SessionFactory.for_app_role(), html_path, tool_output_dir=tmp_path)

    assert summary.imports_attempted == 1
    assert summary.files_not_found == 1
    assert summary.imports_succeeded == 0
    assert summary.other_errors == 0


def test_ingest_log_already_linked_is_skipped_on_rerun(tmp_path, seller_id: str):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = _html(_tr(tool_run_id, f"輸出_{seller_id}", "jp", "待機中", None))
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    factory = SessionFactory.for_app_role()
    first = ingest_log(factory, html_path, tool_output_dir=tmp_path)
    assert first.new_sellers == 1
    assert first.already_linked == 0

    second = ingest_log(factory, html_path, tool_output_dir=tmp_path)
    assert second.already_linked == 1
    assert second.other_errors == 0


def test_ingest_log_reports_parse_errors_without_crashing(tmp_path):
    html = """
    <table><tbody>
      <tr data-seq="123" data-country="uk" data-type="keyword">
        <td class="log-status">待機中</td>
        <td class="log-csv">-</td>
        <td class="log-memo"><textarea class="memo">unresolvable-country</textarea></td>
      </tr>
    </tbody></table>
    """
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    summary = ingest_log(SessionFactory.for_app_role(), html_path, tool_output_dir=tmp_path)

    assert summary.parse_errors == 1
    assert summary.total_rows == 1
    assert "123" in summary.format()
