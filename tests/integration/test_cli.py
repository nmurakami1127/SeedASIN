"""CLIエントリポイント（seller_ledger/tool_intake/seller_performance/asin_collector）の
結合テスト。

main()をsys.argvパッチ経由で実際に呼び出し、SEEDASIN_APP_DATABASE_URL/
SEEDASIN_KPI_REPORT_DATABASE_URL環境変数からのSessionFactory/ReportSessionFactory配線が
実PostgreSQLに正しく到達することを検証する。asin_collector.cliの`collect-brand-page`
（実ブラウザ必須）のみ対象外（tests/integration/test_brand_page_collector.pyが実際の
DB書き込み部分をカバー）。`export-run`はDB読み取り+CSV書き出しのみで実ブラウザ不要の
ため、他のCLIコマンドと同様にここで検証する。

共通フィクスチャ（superuser_role等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import csv
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

import asin_collector.cli as asin_collector_cli
import seller_ledger.cli as seller_ledger_cli
import seller_performance.cli as seller_performance_cli
import tool_intake.cli as tool_intake_cli
from db.session import SessionFactory
from tests.integration.conftest import role_url

pytestmark = pytest.mark.integration

_TOOL_HEADER = (
    "ASIN,バリエーション,商品名,出品者数,ブランド,最安値,重量（kg）,"
    "体積重量（縦×横×高さ/5000）,大カテゴリー名,大カテゴリー順位,中カテゴリー名,"
    "中カテゴリー順位,最安値セラーの出品国,品番,型番,日本出品者数,日本最安値,"
    "日本大カテゴリー名,日本大カテゴリー順位,日本中カテゴリー名,日本中カテゴリー順位,"
    "日米差益,,,取得日時,"
)

_WEEKLY_US_HEADER = (
    "ASIN,画像,商品名,利益,売値/最安値,FBA出荷,送料,諸経費,"
    "幅インチ、長さインチ、高さインチ,在庫数,登録日時,SKU,メーカー,日本最安値,出品価格,"
    "更新日時,販売手数料,米国最安値,ツール外出品,日本Amazon,米国Amazon"
)


@pytest.fixture(autouse=True)
def _app_role_env(monkeypatch, superuser_url: str):
    monkeypatch.setenv("SEEDASIN_APP_DATABASE_URL", role_url(superuser_url, "app_role"))
    monkeypatch.setenv("SEEDASIN_KPI_REPORT_DATABASE_URL", role_url(superuser_url, "kpi_report_role"))
    monkeypatch.setenv("SEEDASIN_ADMIN_DATABASE_URL", role_url(superuser_url, "admin_role"))


def test_seller_ledger_register_and_link_tool_run_via_cli(monkeypatch, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "register", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()

    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_ledger.cli",
            "link-tool-run",
            "--marketplace=US",
            f"--seller-id={seller_id}",
            f"--tool-run-id={tool_run_id}",
        ],
    )
    seller_ledger_cli.main()

    with app_role_factory.get_session("US") as conn:
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"),
            {"sid": seller_id},
        ).fetchall()
        assert len(seller_row) == 1

        link_row = conn.execute(
            text("SELECT seller_id FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).one()
        assert link_row.seller_id == seller_id


def test_seller_ledger_link_tool_run_duplicate_via_cli_shows_friendly_message(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    """2026-09-19: seller_ledger.cli link-tool-run単体（対話式メニューを介さない標準
    コマンド）は、以前は重複時にDuplicateToolRunError/IntegrityErrorをキャッチしておらず
    生のトレースバックでクラッシュしていた。対話式メニューcli.pyの_do_link_tool_run()と
    同じ「既に記録されています」メッセージを表示するよう修正した（docs/DEVIATIONS.md
    #50参照。ユーザー報告のバグ調査中に発見した関連の不整合）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "register", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()

    link_argv = [
        "seller_ledger.cli",
        "link-tool-run",
        "--marketplace=US",
        f"--seller-id={seller_id}",
        f"--tool-run-id={tool_run_id}",
    ]
    monkeypatch.setattr("sys.argv", link_argv)
    seller_ledger_cli.main()  # 1回目: 成功

    monkeypatch.setattr("sys.argv", link_argv)
    seller_ledger_cli.main()  # 2回目: 例外を送出せず、友好的なメッセージを表示すること

    out = capsys.readouterr().out
    assert f"既にtool_run_id={tool_run_id}が記録されています（スキップしました）。" in out

    with app_role_factory.get_session("US") as conn:
        count = conn.execute(
            text("SELECT count(*) FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).scalar_one()
        assert count == 1  # 2回目の試行で重複INSERTされていないこと


def test_seller_ledger_mark_storefront_blocked_via_cli(monkeypatch, app_role_factory: SessionFactory, capsys):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_ledger.cli",
            "mark-storefront-blocked",
            f"--seller-id={seller_id}",
            "--seller-name=Example Store",
            "--note=同業者利用エラー",
        ],
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "登録しました" in out
    assert "同業者利用" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT reason, seller_name FROM storefront_blocked_seller WHERE seller_id=:sid"),
            {"sid": seller_id},
        ).one()
        assert row.reason == "COMPETITOR_SAME_SERVICE"
        assert row.seller_name == "Example Store"


def test_seller_ledger_mark_storefront_blocked_reports_already_registered_via_cli(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    monkeypatch.setattr(
        "sys.argv", ["seller_ledger.cli", "mark-storefront-blocked", f"--seller-id={seller_id}"]
    )
    seller_ledger_cli.main()
    capsys.readouterr()  # 1回目の出力は読み捨てる

    seller_ledger_cli.main()  # 同じseller_idで2回目
    out = capsys.readouterr().out

    assert "既に登録済みです" in out
    assert "同業者利用" in out


def _seed_seller_link_and_output(conn, marketplace: str, seller_id: str, tool_run_id: str, asin: str) -> None:
    conn.execute(
        text(
            "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
            "known_at_reliable, known_before_seedasin, last_seen_at) "
            "VALUES (:mp, :sid, now(), true, false, now())"
        ),
        {"mp": marketplace, "sid": seller_id},
    )
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) "
            "VALUES (:mp, :sid, :rid, 'SELLER_PAGE')"
        ),
        {"mp": marketplace, "sid": seller_id, "rid": tool_run_id},
    )
    conn.execute(
        text(
            "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
            "VALUES (:mp, :rid, :asin, now())"
        ),
        {"mp": marketplace, "rid": tool_run_id, "asin": asin},
    )


def test_seller_ledger_delete_seller_via_cli_confirmed(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setattr(
        "sys.argv", ["seller_ledger.cli", "delete-seller", "--marketplace=US", f"--seller-id={seller_id}"]
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "本当に削除しますか？削除される件数" in out
    assert "known_seller_master 1件" in out
    assert "削除しました" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert row == []


def test_seller_ledger_delete_seller_via_cli_declined(monkeypatch, app_role_factory: SessionFactory, capsys):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr(
        "sys.argv", ["seller_ledger.cli", "delete-seller", "--marketplace=US", f"--seller-id={seller_id}"]
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "削除を中止しました。" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(row) == 1  # 削除されていない


def test_seller_ledger_delete_seller_reports_no_data_without_prompting(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(AssertionError("should not prompt")))
    monkeypatch.setattr(
        "sys.argv", ["seller_ledger.cli", "delete-seller", "--marketplace=US", "--seller-id=NOSUCHSELLER"]
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "該当するデータがありません。" in out


def test_seller_ledger_delete_tool_run_via_cli_confirmed(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setattr(
        "sys.argv", ["seller_ledger.cli", "delete-tool-run", "--marketplace=US", f"--tool-run-id={tool_run_id}"]
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "tool_execution_link 1件" in out
    assert "削除しました" in out

    with app_role_factory.get_session("US") as conn:
        link_row = conn.execute(
            text("SELECT 1 FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).fetchall()
        assert link_row == []
        # known_seller_masterは残る
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(seller_row) == 1


def test_seller_ledger_delete_storefront_block_via_cli_confirmed(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "COMPETITOR_SAME_SERVICE", note="間違えて登録した"
        )

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "delete-storefront-block", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "本当に削除しますか？（理由: 同業者利用、登録日:" in out
    assert "メモ: 間違えて登録した" in out
    assert "削除しました" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is None


def test_seller_ledger_delete_storefront_block_via_cli_declined(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "delete-storefront-block", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "削除を中止しました。" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None  # 削除されていない


def test_seller_ledger_delete_storefront_block_reports_not_registered_without_prompting(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(AssertionError("should not prompt")))
    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "delete-storefront-block", "--marketplace=US", "--seller-id=NOSUCHSELLER"],
    )
    seller_ledger_cli.main()

    out = capsys.readouterr().out
    assert "は登録されていません。" in out


def test_tool_intake_import_tool_output_via_cli(monkeypatch, tmp_path, app_role_factory: SessionFactory):
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{tool_run_id}_output.csv"
    cells = [""] * 26
    cells[0] = asin
    cells[24] = "2026-09-01 12:00:00"
    path.write_text(_TOOL_HEADER + "\n" + ",".join(cells) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "tool_intake.cli",
            "import-tool-output",
            "--marketplace=US",
            f"--tool-run-id={tool_run_id}",
            f"--file={path}",
        ],
    )
    tool_intake_cli.main()

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(text("SELECT 1 FROM tool_output_asin WHERE asin=:asin"), {"asin": asin}).fetchall()
        assert len(row) == 1


def test_tool_intake_import_tool_output_auto_detects_file_via_cli(
    monkeypatch, tmp_path, capsys, app_role_factory: SessionFactory
):
    """--fileを省略した場合、自動検出されたファイルがそのまま取込に使われること
    （docs/DEVIATIONS.md #37参照）。"""
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{tool_run_id}_output.csv"
    cells = [""] * 26
    cells[0] = asin
    cells[24] = "2026-09-01 12:00:00"
    path.write_text(_TOOL_HEADER + "\n" + ",".join(cells) + "\n", encoding="utf-8")

    monkeypatch.setattr("tool_intake.cli.find_tool_output_files", lambda mp, rid: [path])
    monkeypatch.setattr(
        "sys.argv",
        ["tool_intake.cli", "import-tool-output", "--marketplace=US", f"--tool-run-id={tool_run_id}"],
    )
    tool_intake_cli.main()

    assert "自動検出しました" in capsys.readouterr().out
    with app_role_factory.get_session("US") as conn:
        row = conn.execute(text("SELECT 1 FROM tool_output_asin WHERE asin=:asin"), {"asin": asin}).fetchall()
        assert len(row) == 1


def test_tool_intake_import_weekly_listing_via_cli(monkeypatch, tmp_path, app_role_factory: SessionFactory):
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / "listing-KW02887.csv"
    cells = [""] * 21
    cells[0] = asin
    cells[10] = "2026-09-01 10:00:00"  # 登録日時（snapshot_dateの元になる、#33参照）
    cells[11] = "USNEW1"
    path.write_text(_WEEKLY_US_HEADER + "\n" + ",".join(cells) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["tool_intake.cli", "import-weekly-listing", "--marketplace=US", f"--file={path}"],
    )
    tool_intake_cli.main()

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT snapshot_date FROM weekly_listing_snapshot WHERE asin=:asin"), {"asin": asin}
        ).one()
        assert row.snapshot_date == date(2026, 9, 1)


def test_tool_intake_sync_pending_imports_when_file_found_via_cli(
    monkeypatch, tmp_path, capsys, app_role_factory: SessionFactory
):
    """tool_execution_linkにあるがtool_output_asinが未取込のtool_run_idを検出し、
    ファイルが見つかれば自動取込すること（2026-09-16のユーザー確認）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / f"{tool_run_id}_output.csv"
    cells = [""] * 26
    cells[0] = asin
    cells[24] = "2026-09-01 12:00:00"
    path.write_text(_TOOL_HEADER + "\n" + ",".join(cells) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "register", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()
    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_ledger.cli",
            "link-tool-run",
            "--marketplace=US",
            f"--seller-id={seller_id}",
            f"--tool-run-id={tool_run_id}",
        ],
    )
    seller_ledger_cli.main()

    # 同じUS marketplaceの他のテストが残した「未取込」のtool_execution_linkも
    # find_pending()の対象に含まれ得るため（DBはテストモジュール単位でしかリセットしない）、
    # このtool_run_idにだけファイルが見つかったことにする。
    monkeypatch.setattr(
        "tool_intake.pending_sync.find_tool_output_files",
        lambda mp, rid: [path] if rid == tool_run_id else [],
    )
    monkeypatch.setattr("sys.argv", ["tool_intake.cli", "sync-pending", "--marketplace=US"])
    tool_intake_cli.main()

    out = capsys.readouterr().out
    assert f"tool_run_id={tool_run_id}" in out
    with app_role_factory.get_session("US") as conn:
        row = conn.execute(text("SELECT 1 FROM tool_output_asin WHERE asin=:asin"), {"asin": asin}).fetchall()
        assert len(row) == 1


def test_tool_intake_sync_pending_reports_not_found_via_cli(monkeypatch, capsys):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        "sys.argv",
        ["seller_ledger.cli", "register", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_ledger_cli.main()
    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_ledger.cli",
            "link-tool-run",
            "--marketplace=US",
            f"--seller-id={seller_id}",
            f"--tool-run-id={tool_run_id}",
        ],
    )
    seller_ledger_cli.main()

    monkeypatch.setattr("tool_intake.pending_sync.find_tool_output_files", lambda mp, rid: [])
    monkeypatch.setattr("sys.argv", ["tool_intake.cli", "sync-pending", "--marketplace=US"])
    tool_intake_cli.main()

    out = capsys.readouterr().out
    assert "自動取込できた: 0件" in out
    assert f"まだ未検出: " in out
    assert f"tool_run_id={tool_run_id}" in out


def test_seller_performance_attribute_via_cli(monkeypatch, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('US', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
                "VALUES ('US', :rid, :asin, '2026-08-01 10:00:00')"
            ),
            {"rid": tool_run_id, "asin": asin},
        )
        conn.execute(
            text(
                "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) "
                "VALUES ('US', :asin, 'USNEW1', current_date)"
            ),
            {"asin": asin},
        )

    monkeypatch.setattr("sys.argv", ["seller_performance.cli", "attribute", "--marketplace=US"])
    seller_performance_cli.main()

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT seller_id FROM seller_attribution WHERE marketplace='US' AND asin=:asin"), {"asin": asin}
        ).one()
        assert row.seller_id == seller_id


def test_seller_performance_recommend_resolves_threshold_from_config(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["seller_performance.cli", "recommend", "--marketplace=US"])
    seller_performance_cli.main()

    out = capsys.readouterr().out
    assert "lookback_days=90" in out
    assert "min_days_since_last=15" in out


def test_seller_performance_recommend_min_days_since_last_override_via_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["seller_performance.cli", "recommend", "--marketplace=US", "--min-days-since-last=30"]
    )
    seller_performance_cli.main()

    out = capsys.readouterr().out
    assert "min_days_since_last=30" in out


def _read_recommend_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_seller_performance_recommend_shows_new_fields_via_cli(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    """recommendの出力CSVに合計出品成果・最終取得日・前回比・直近90日以内の取得日数が
    含まれること（ユーザー依頼、docs/DEVIATIONS.md #43参照。2026-10-07以降は画面表示
    ではなくCSV出力になったため、CSVの内容を検証する、docs/DEVIATIONS.md #53参照）。"""
    from seller_performance.attribution import AttributionService

    monkeypatch.chdir(tmp_path)
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('US', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE', now() - interval '20 days')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) VALUES "
                "('US', :rid, :asin1, '2026-08-01 10:00:00'), ('US', :rid, :asin2, '2026-08-01 10:00:00')"
            ),
            {"rid": tool_run_id, "asin1": asin1, "asin2": asin2},
        )
        conn.execute(
            text(
                "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) VALUES "
                "('US', :asin1, 'USNEW1', current_date - interval '10 days'), "
                "('US', :asin2, 'USNEW2', current_date - interval '5 days')"
            ),
            {"asin1": asin1, "asin2": asin2},
        )

    with app_role_factory.get_session("US") as conn:
        AttributionService().attribute(conn, "US")

    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_performance.cli",
            "recommend",
            "--marketplace=US",
            "--lookback-days=90",
            "--min-days-since-last=15",
        ],
    )
    seller_performance_cli.main()

    out = capsys.readouterr().out
    assert "recommendations=" in out
    last_line = out.splitlines()[-1]
    assert "に書き出しました" in last_line
    output_path = Path(last_line.split("件を", 1)[1].rsplit("に書き出しました", 1)[0])
    rows = _read_recommend_csv(output_path)
    matching = [r for r in rows if r["seller_id"] == seller_id]
    assert len(matching) == 1
    row = matching[0]
    assert row["最終取得日"] != ""
    assert row["合計出品成果"] == "2"
    assert row["前回比"] == ""  # 取得日が1日しかないため比較不可


def test_seller_performance_recommend_shows_previous_delta_with_two_fetch_days_via_cli(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    """取得日が2日以上あるセラーで、実際にDBを経由した前回比がCSVに正しく出力されること。
    PostgreSQLのSUM(bigint)がnumeric（psycopgではDecimal）になり、以前のCLI画面出力の
    {:+d}フォーマットで実際にValueErrorになっていたのを実データでの動作確認で発見した
    （docs/DEVIATIONS.md #43参照）——単体テストのモックだけでは検出できなかった実バグの
    ため、DBを経由するこの経路を明示的にテストする（2026-10-07以降はCSV出力、
    previous_deltaは生の整数のため同種の型変換バグはCSV側には存在しないが、
    回帰確認として引き続きDB経由の値を検証する、docs/DEVIATIONS.md #53参照）。"""
    from seller_performance.attribution import AttributionService

    monkeypatch.chdir(tmp_path)
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    old_run = f"run-{uuid.uuid4().hex[:8]}"
    recent_run = f"run-{uuid.uuid4().hex[:8]}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin3 = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('US', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid1, 'SELLER_PAGE', now() - interval '60 days'), "
                "('US', :sid, :rid2, 'SELLER_PAGE', now() - interval '20 days')"
            ),
            {"sid": seller_id, "rid1": old_run, "rid2": recent_run},
        )
        conn.execute(
            text(
                "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) VALUES "
                "('US', :rid1, :asin1, '2026-07-01 10:00:00'), "
                "('US', :rid2, :asin2, '2026-08-20 10:00:00'), "
                "('US', :rid2, :asin3, '2026-08-20 10:00:00')"
            ),
            {"rid1": old_run, "rid2": recent_run, "asin1": asin1, "asin2": asin2, "asin3": asin3},
        )
        conn.execute(
            text(
                "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) VALUES "
                "('US', :asin1, 'USNEW1', current_date - interval '50 days'), "
                "('US', :asin2, 'USNEW2', current_date - interval '10 days'), "
                "('US', :asin3, 'USNEW3', current_date - interval '5 days')"
            ),
            {"asin1": asin1, "asin2": asin2, "asin3": asin3},
        )

    with app_role_factory.get_session("US") as conn:
        AttributionService().attribute(conn, "US")

    monkeypatch.setattr(
        "sys.argv",
        [
            "seller_performance.cli",
            "recommend",
            "--marketplace=US",
            "--lookback-days=90",
            "--min-days-since-last=15",
        ],
    )
    seller_performance_cli.main()  # ValueErrorを送出しないこと（実バグの再発防止）

    out = capsys.readouterr().out
    last_line = out.splitlines()[-1]
    output_path = Path(last_line.split("件を", 1)[1].rsplit("に書き出しました", 1)[0])
    rows = _read_recommend_csv(output_path)
    matching = [r for r in rows if r["seller_id"] == seller_id]
    assert len(matching) == 1
    assert matching[0]["前回比"] == "1"  # 直近の取得日(asin2+asin3=2件) - その1つ前(asin1=1件) = +1


def test_seller_performance_history_via_cli(monkeypatch, app_role_factory: SessionFactory, capsys):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('US', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE', '2026-05-22 10:00:00+00')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )

    monkeypatch.setattr(
        "sys.argv",
        ["seller_performance.cli", "history", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_performance_cli.main()

    out = capsys.readouterr().out
    assert f"セラー: {seller_id} (marketplace=US)" in out
    # 取得日はrequested_at基準、tool_output_asinは未取込、統合ツールIDも表示される。
    assert f"2026-05-22      {tool_run_id}      -      0件" in out
    assert "合計取得回数: 1回 / 合計出品成果: 0件" in out


def test_seller_performance_history_shows_blocked_notice_via_cli(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO known_seller_master (marketplace, seller_id, first_known_at, "
                "known_at_reliable, known_before_seedasin, last_seen_at) "
                "VALUES ('US', :sid, now(), true, false, now())"
            ),
            {"sid": seller_id},
        )
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE', '2026-05-22 10:00:00+00')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )
        conn.execute(
            text(
                "INSERT INTO storefront_blocked_seller (seller_id, reason, note) "
                "VALUES (:sid, 'PERMANENTLY_EXCLUDED', '低単価')"
            ),
            {"sid": seller_id},
        )

    monkeypatch.setattr(
        "sys.argv",
        ["seller_performance.cli", "history", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_performance_cli.main()

    out = capsys.readouterr().out
    assert "※このセラーは統合ツールのストアフロントURLから取得できません" in out
    assert "今後も取得しない" in out
    assert "メモ: 低単価" in out
    # 禁止セラーであっても、警告だけで終わらず通常の取得履歴も続けて表示されること
    # （ユーザー指摘の不具合修正、docs/DEVIATIONS.md #36参照）。
    assert f"2026-05-22      {tool_run_id}      -      0件" in out
    assert "合計取得回数: 1回" in out


def test_seller_performance_history_reports_not_found_via_cli(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["seller_performance.cli", "history", "--marketplace=US", "--seller-id=NOSUCHSELLER"],
    )
    with pytest.raises(SystemExit, match="該当するセラーが見つかりません"):
        seller_performance_cli.main()


def test_seller_performance_history_blocked_seller_with_no_history_shows_warning_and_empty_history_via_cli(
    monkeypatch, app_role_factory: SessionFactory, capsys
):
    """tool_execution_linkに記録が無くても、storefront_blocked_sellerに登録済みなら
    SystemExitで終わらせず、警告表示に続けて「取得履歴はまだありません」を表示すること
    （2026-09-10仕様修正、docs/DEVIATIONS.md #36参照。以前はSystemExitで打ち切っていた）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO storefront_blocked_seller (seller_id, reason) "
                "VALUES (:sid, 'PERMANENTLY_EXCLUDED')"
            ),
            {"sid": seller_id},
        )

    monkeypatch.setattr(
        "sys.argv",
        ["seller_performance.cli", "history", "--marketplace=US", f"--seller-id={seller_id}"],
    )
    seller_performance_cli.main()  # SystemExitを送出しないこと

    out = capsys.readouterr().out
    assert "※このセラーは統合ツールのストアフロントURLから取得できません" in out
    assert "取得履歴はまだありません。" in out


def _seed_collector_run(app_role_factory: SessionFactory, marketplace: str, asins: list[str]) -> int:
    with app_role_factory.get_session(marketplace) as conn, conn.begin():
        run_id = conn.execute(
            text(
                "INSERT INTO collector_run (marketplace, run_type, target, status) "
                "VALUES (:mp, 'BRAND_PAGE', 'https://example.com', 'COMPLETED') RETURNING id"
            ),
            {"mp": marketplace},
        ).scalar_one()
        for asin in asins:
            conn.execute(
                text(
                    "INSERT INTO collector_asin (marketplace, collector_run_id, asin) "
                    "VALUES (:mp, :run_id, :asin)"
                ),
                {"mp": marketplace, "run_id": run_id, "asin": asin},
            )
    return run_id


def test_export_run_with_explicit_id(monkeypatch, tmp_path, app_role_factory: SessionFactory, capsys):
    run_id = _seed_collector_run(app_role_factory, "US", ["B0TESTASN1", "B0TESTASN2"])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["asin_collector.cli", "export-run", f"--collector-run-id={run_id}"])
    asin_collector_cli.main()

    out = capsys.readouterr().out
    relative_path = Path("exports") / "collected_asins" / f"collected_asins_{run_id}.csv"
    assert f"2件を{relative_path}に書き出しました" in out
    output_path = tmp_path / relative_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    # ヘッダー行は付けない（ユーザー確認済み、2026-09-06）。
    assert content.splitlines() == ["B0TESTASN1", "B0TESTASN2"]


def test_export_run_defaults_to_most_recent_across_marketplaces(
    monkeypatch, tmp_path, app_role_factory: SessionFactory, capsys
):
    _seed_collector_run(app_role_factory, "US", ["B0OLDASIN1"])
    latest_run_id = _seed_collector_run(app_role_factory, "JP", ["B0NEWASIN1"])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["asin_collector.cli", "export-run"])
    asin_collector_cli.main()

    out = capsys.readouterr().out
    relative_path = Path("exports") / "collected_asins" / f"collected_asins_{latest_run_id}.csv"
    output_path = tmp_path / relative_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert content.splitlines() == ["B0NEWASIN1"]
    assert f"1件を{relative_path}に書き出しました" in out


def test_export_run_creates_export_dir_if_missing(monkeypatch, tmp_path, app_role_factory: SessionFactory):
    """exports/collected_asins/ が存在しない状態から自動作成されること。"""
    run_id = _seed_collector_run(app_role_factory, "US", ["B0TESTASN1"])

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "exports").exists()

    monkeypatch.setattr("sys.argv", ["asin_collector.cli", "export-run", f"--collector-run-id={run_id}"])
    asin_collector_cli.main()

    assert (tmp_path / "exports" / "collected_asins" / f"collected_asins_{run_id}.csv").exists()


def test_export_run_nonexistent_id_exits(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["asin_collector.cli", "export-run", "--collector-run-id=999999999"])
    with pytest.raises(SystemExit):
        asin_collector_cli.main()


def test_tool_intake_ingest_log_via_cli(monkeypatch, tmp_path, app_role_factory: SessionFactory, capsys):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    html = f"""
    <table><tbody>
      <tr data-seq="{tool_run_id}" data-country="jp" data-type="keyword">
        <td class="log-status">待機中</td>
        <td class="log-csv">-</td>
        <td class="log-memo"><textarea class="memo">輸出_{seller_id}</textarea></td>
      </tr>
    </tbody></table>
    """
    html_path = tmp_path / "log.html"
    html_path.write_text(html, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["tool_intake.cli", "ingest-log", f"--file={html_path}"])
    tool_intake_cli.main()

    out = capsys.readouterr().out
    assert "新規1件" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(row) == 1
