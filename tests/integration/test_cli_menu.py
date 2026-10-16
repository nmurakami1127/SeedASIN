"""ルート直下のcli.py（対話式メニュー）の結合テスト: 実PostgreSQL 16に対して、
builtins.inputへ回答列をmonkeypatchした上でmain()を実際に呼び出し、各メニューの
正常系・分岐・エラー時の継続を検証する。

docs/DEVIATIONS.md #35参照。共通フィクスチャ（superuser_url等）は
tests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

import cli as interactive_cli
from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_ledger.tool_run_link import ToolRunLinkService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _app_role_env(monkeypatch, superuser_url: str):
    from tests.integration.conftest import role_url

    monkeypatch.setenv("SEEDASIN_APP_DATABASE_URL", role_url(superuser_url, "app_role"))
    monkeypatch.setenv("SEEDASIN_ADMIN_DATABASE_URL", role_url(superuser_url, "admin_role"))


def _run_with_inputs(monkeypatch, *answers: str) -> None:
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))
    interactive_cli.main()


def test_main_menu_displays_and_exits_on_zero(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "0")
    out = capsys.readouterr().out
    assert "何をしますか？" in out
    assert "1. セラー/ブランド/キーワードの取得履歴を見る" in out
    assert "終了します。" in out


def test_invalid_choice_shows_message_and_returns_to_menu(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "99", "0")
    out = capsys.readouterr().out
    assert "該当する番号がありません。" in out


def test_history_blocked_seller_with_no_entries_still_offers_options(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """禁止セラーであっても、警告表示だけで終了させず、通常の3択を引き続き提示すること
    （2026-09-10仕様修正、docs/DEVIATIONS.md #36参照。以前はここでメインメニューへ戻っていた）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

    # "1"(履歴を見る), marketplace, seller-id, (3択のうち"3"), "0"
    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "統合ツールのストアフロントURLから取得できません" in out
    assert "取得履歴はまだありません。" in out
    # 3択が実際に提示され、「3」が処理されたこと（プロンプト文字列自体はinput()の引数として
    # 渡るだけで標準出力には現れないため、print()される選択肢の見出しと処理結果で確認する）。
    assert "1. 統合ツールのIDとひもづける" in out
    assert "2. このセラーは取得できないと分かった" in out
    assert "何もしません。" in out


def test_history_blocked_seller_with_entries_shows_both_warning_and_history(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """禁止セラーであっても取得履歴が実在する場合（独自プログラム経由のASINリスト登録等）、
    警告と通常の履歴表示の両方を見せ、3択も提示すること。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE")
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE', '2026-05-22 10:00:00+00')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "統合ツールのストアフロントURLから取得できません" in out
    assert f"2026-05-22      {tool_run_id}      -      0件" in out
    assert "合計取得回数: 1回" in out
    assert "1. 統合ツールのIDとひもづける" in out
    assert "何もしません。" in out


def test_history_blocked_seller_shows_note_when_present(monkeypatch, capsys, app_role_factory: SessionFactory):
    """noteが記録されている場合、警告メッセージに続けてメモの中身を表示すること
    （ユーザー指摘の不具合、docs/DEVIATIONS.md #41参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note="低単価")

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "メモ: 低単価" in out


def test_history_blocked_seller_no_longer_offers_deletion_and_keeps_record(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """2026-09-18に削除確認の質問を廃止した後は、警告表示のすぐ後に3択が提示され、
    ブロック記録はそのまま残ること（訂正が必要な場合はseller_ledger.cli
    delete-storefront-blockを使う、ユーザー依頼、docs/DEVIATIONS.md #47参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE")

    # "1"(履歴を見る), marketplace, seller-id, (削除確認は無いので直接3択のうち"3"), "0"
    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "このブロック記録を削除しますか" not in out
    assert "削除しました" not in out
    assert "何もしません。" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None  # 削除されていない


def test_history_unregistered_seller_registers_then_shows_same_three_choices_as_registered(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """未登録セラーでも、登録済みセラーと全く同じ3択が提示されること（以前は未登録の
    場合だけ3択を出さずいきなり統合tool IDの入力を求めていた不具合の修正、
    ユーザー指摘、docs/DEVIATIONS.md #40参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    # "1"(履歴を見る), marketplace, seller-id, (3択のうち"1"を選択), tool-run-id, "0"
    _run_with_inputs(monkeypatch, "1", "US", seller_id, "1", tool_run_id, "0")
    out = capsys.readouterr().out
    assert "はまだ登録されていなかったため、新規登録しました。" in out
    assert "取得履歴がまだありません。" in out
    assert "1. 統合ツールのIDとひもづける" in out
    assert "2. このセラーは取得できないと分かった" in out
    assert "3. 何もしない" in out
    assert f"linked: marketplace=US seller_id={seller_id} tool_run_id={tool_run_id}" in out

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


def test_history_unregistered_seller_can_also_choose_mark_as_blocked(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """未登録セラーでも、3択のうち「2. このセラーは取得できないと分かった」を選べること
    （選択肢1だけでなく2・3も未登録セラーに対して機能することの確認）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    # "1"(履歴を見る), marketplace, seller-id, (3択のうち"2"を選択), 理由=2(COMPETITOR_SAME_SERVICE), メモ(空欄), "0"
    _run_with_inputs(monkeypatch, "1", "US", seller_id, "2", "2", "", "0")
    out = capsys.readouterr().out
    assert "はまだ登録されていなかったため、新規登録しました。" in out
    assert "登録しました: seller_id" in out
    assert "同業者利用" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "COMPETITOR_SAME_SERVICE"


def test_history_option_2_can_also_choose_permanently_excluded(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    """メニュー1「2」でも、メニュー9と同様にPERMANENTLY_EXCLUDEDを選べること
    （以前はCOMPETITOR_SAME_SERVICE固定だった、docs/DEVIATIONS.md #41参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "2", "1", "低単価", "0")
    out = capsys.readouterr().out
    assert "今後も取得しない" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.note == "低単価"


def test_history_option_2_can_also_choose_retry_later(monkeypatch, capsys, app_role_factory: SessionFactory):
    """メニュー1「2」で新設のRETRY_LATER（3番目の選択肢）も選べること
    （ユーザー依頼、docs/DEVIATIONS.md #47参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "2", "3", "在庫切れとのことなので後日再挑戦", "0")
    out = capsys.readouterr().out
    assert "一時的に取得できない" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "RETRY_LATER"
    assert info.note == "在庫切れとのことなので後日再挑戦"


def test_history_option_2_invalid_reason_choice_shows_message(monkeypatch, capsys, app_role_factory: SessionFactory):
    """3択（RETRY_LATER追加、docs/DEVIATIONS.md #47参照）になったため、無効な選択肢は
    "4"を使う。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "2", "4", "0")
    out = capsys.readouterr().out
    assert "1〜3を入力してください。" in out


def test_history_unregistered_seller_can_choose_do_nothing(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "はまだ登録されていなかったため、新規登録しました。" in out
    assert "何もしません。" in out

    with app_role_factory.get_session("US") as conn:
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(seller_row) == 1  # 登録自体はされている


def test_history_registered_seller_with_no_entries_shows_message_and_options(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)

    _run_with_inputs(monkeypatch, "1", "US", seller_id, "3", "0")
    out = capsys.readouterr().out
    assert "登録済みですが、取得履歴がまだありません" in out
    assert "何もしません。" in out


def test_history_registered_seller_with_entries_shows_history_then_option_2_blocks(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        conn.execute(
            text(
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE', '2026-05-22 10:00:00+00')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )

    # "1"(履歴を見る), marketplace, seller-id, (3択のうち"2"を選択), 理由=2(COMPETITOR_SAME_SERVICE), メモ, "0"
    _run_with_inputs(monkeypatch, "1", "US", seller_id, "2", "2", "同業者らしいのでメモ", "0")
    out = capsys.readouterr().out
    assert f"セラー: {seller_id} (marketplace=US)" in out
    assert "登録しました" in out
    assert "同業者利用" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "COMPETITOR_SAME_SERVICE"


def test_link_tool_run_menu_standalone(monkeypatch, capsys, app_role_factory: SessionFactory):
    """発見経路（source）の質問は廃止済み（ユーザー依頼、docs/DEVIATIONS.md #48参照）。
    tool_execution_link.sourceはNULLのまま記録される。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

    _run_with_inputs(monkeypatch, "2", "JP", seller_id, tool_run_id, "0")
    out = capsys.readouterr().out
    assert f"linked: marketplace=JP seller_id={seller_id} tool_run_id={tool_run_id}" in out

    with app_role_factory.get_session("JP") as conn:
        row = conn.execute(
            text("SELECT source FROM tool_execution_link WHERE marketplace='JP' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).one()
        assert row.source is None


def test_link_tool_run_duplicate_is_handled_gracefully(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        SellerService().register(conn, "US", seller_id)
        ToolRunLinkService().record(conn, "US", seller_id, tool_run_id)

    _run_with_inputs(monkeypatch, "2", "US", seller_id, tool_run_id, "0")
    out = capsys.readouterr().out
    assert f"既にtool_run_id={tool_run_id}が記録されています" in out


def test_import_tool_output_menu_falls_back_to_manual_path_when_no_auto_detect_match(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    """自動検出が0件（実運用フォルダに一致ファイルが無いランダムなtool-run-id）の場合、
    従来通りファイルパスの手入力にフォールバックすること（docs/DEVIATIONS.md #37参照）。"""
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / "output.csv"
    path.write_text(f"ASIN,取得日時\n{asin},2026-09-01 12:00:00\n", encoding="utf-8")

    _run_with_inputs(monkeypatch, "3", "US", tool_run_id, str(path), "0")
    out = capsys.readouterr().out
    assert "自動検出できませんでした。ファイルパスを入力してください。" in out
    assert "total=1 success=1 error=0" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_run_id, "asin": asin},
        ).fetchall()
        assert len(row) == 1


def test_import_tool_output_menu_auto_detects_single_match(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / f"{tool_run_id}_someone@example.com.csv"
    path.write_text(f"ASIN,取得日時\n{asin},2026-09-01 12:00:00\n", encoding="utf-8")

    monkeypatch.setattr("cli.find_tool_output_files", lambda mp, rid: [path])

    # ファイルパスの入力を求められないため、"3", marketplace, tool-run-id, "0" のみで足りる。
    _run_with_inputs(monkeypatch, "3", "US", tool_run_id, "0")
    out = capsys.readouterr().out
    assert f"ファイルを自動検出しました: {path}" in out
    assert "total=1 success=1 error=0" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_run_id, "asin": asin},
        ).fetchall()
        assert len(row) == 1


def test_import_tool_output_menu_lets_user_choose_among_multiple_matches(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
    asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
    path1 = tmp_path / f"{tool_run_id}_a@example.com.csv"
    path2 = tmp_path / f"{tool_run_id}_b@example.com.csv"
    path1.write_text(f"ASIN,取得日時\n{asin1},2026-09-01 12:00:00\n", encoding="utf-8")
    path2.write_text(f"ASIN,取得日時\n{asin2},2026-09-02 12:00:00\n", encoding="utf-8")

    monkeypatch.setattr("cli.find_tool_output_files", lambda mp, rid: [path1, path2])

    # "3", marketplace, tool-run-id, (一覧から2番目を選択), "0"
    _run_with_inputs(monkeypatch, "3", "US", tool_run_id, "2", "0")
    out = capsys.readouterr().out
    assert "複数のファイルが見つかりました。" in out
    assert f"1. {path1}" in out
    assert f"2. {path2}" in out

    with app_role_factory.get_session("US") as conn:
        row1 = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_run_id, "asin": asin1},
        ).fetchall()
        row2 = conn.execute(
            text("SELECT 1 FROM tool_output_asin WHERE marketplace='US' AND tool_run_id=:rid AND asin=:asin"),
            {"rid": tool_run_id, "asin": asin2},
        ).fetchall()
        assert row1 == []  # 選ばなかった方は取り込まれていない
        assert len(row2) == 1  # 選んだ方（2番目=path2）が取り込まれている


def test_import_tool_output_bad_file_path_shows_error_and_returns_to_menu(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "3", "US", "run-x", "C:\\does\\not\\exist.csv", "0")
    out = capsys.readouterr().out
    assert "エラーが発生しました" in out
    assert "何をしますか？" in out  # メインメニューへ戻っている


def test_collect_brand_page_unsupported_domain_shows_error(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "4", "https://example.com/search", "0")
    out = capsys.readouterr().out
    assert "エラー:" in out
    assert "marketplaceを判定できません" in out


def test_import_weekly_listing_menu(monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory):
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    header = (
        "ASIN,画像,商品名,利益,売値/最安値,FBA出荷,送料,諸経費,"
        "幅インチ、長さインチ、高さインチ,在庫数,登録日時,SKU,メーカー,日本最安値,出品価格,"
        "更新日時,販売手数料,米国最安値,ツール外出品,日本Amazon,米国Amazon"
    )
    cells = [""] * 21
    cells[0] = asin
    cells[10] = "2026-09-01 10:00:00"
    cells[11] = "USNEW1"
    path = tmp_path / "listing.csv"
    path.write_text(header + "\n" + ",".join(cells) + "\n", encoding="utf-8")

    _run_with_inputs(monkeypatch, "6", "US", str(path), "0")
    out = capsys.readouterr().out
    assert "total=1 success=1 error=0" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT snapshot_date FROM weekly_listing_snapshot WHERE marketplace='US' AND asin=:asin"),
            {"asin": asin},
        ).one()
        assert row.snapshot_date == date(2026, 9, 1)


def test_check_continuous_sellers_menu_single_marketplace(monkeypatch, capsys, app_role_factory: SessionFactory):
    _run_with_inputs(monkeypatch, "5", "1", "0")
    out = capsys.readouterr().out
    assert "attributed" in out
    assert "marketplace=US" in out
    assert "recommendations=" in out
    assert "marketplace=JP" not in out


def test_check_continuous_sellers_menu_both_marketplaces(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "5", "3", "0")
    out = capsys.readouterr().out
    assert out.count("attributed") == 2
    assert "marketplace=US" in out
    assert "marketplace=JP" in out


def test_error_in_one_menu_does_not_prevent_using_another_afterward(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    """1つのメニューでエラーが起きても、アプリ全体は落ちず、続けて別の操作ができること
    （CLAUDE.md 6節のエラー分離をCLI層でも確認する）。"""
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / "output.csv"
    path.write_text(f"ASIN,取得日時\n{asin},2026-09-01 12:00:00\n", encoding="utf-8")

    _run_with_inputs(
        monkeypatch,
        "3", "US", "run-bad", "C:\\does\\not\\exist.csv",  # 1回目: 失敗する
        "3", "US", tool_run_id, str(path),  # 2回目: 成功する
        "0",
    )
    out = capsys.readouterr().out
    assert "エラーが発生しました" in out
    assert "total=1 success=1 error=0" in out


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


def test_delete_seller_menu_confirmed(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    _run_with_inputs(monkeypatch, "7", "US", seller_id, "y", "0")
    out = capsys.readouterr().out
    assert "本当に削除しますか？削除される件数" in out
    assert "削除しました" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert row == []


def test_delete_seller_menu_declined_keeps_data(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    _run_with_inputs(monkeypatch, "7", "US", seller_id, "n", "0")
    out = capsys.readouterr().out
    assert "削除を中止しました。" in out

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(row) == 1


def test_delete_seller_menu_reports_no_data(monkeypatch, capsys):
    _run_with_inputs(monkeypatch, "7", "US", f"NOSUCH{uuid.uuid4().hex[:8]}", "0")
    out = capsys.readouterr().out
    assert "該当するデータがありません。" in out


def test_delete_tool_run_menu_confirmed_keeps_seller(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        _seed_seller_link_and_output(conn, "US", seller_id, tool_run_id, asin)

    _run_with_inputs(monkeypatch, "8", "US", tool_run_id, "y", "0")
    out = capsys.readouterr().out
    assert "本当に削除しますか？削除される件数" in out
    assert "削除しました" in out

    with app_role_factory.get_session("US") as conn:
        link_row = conn.execute(
            text("SELECT 1 FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:rid"),
            {"rid": tool_run_id},
        ).fetchall()
        assert link_row == []
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert len(seller_row) == 1


def test_mark_storefront_blocked_menu_permanently_excluded(monkeypatch, capsys, app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    # "9", marketplace, seller-id, 理由=1(PERMANENTLY_EXCLUDED), メモ(空欄), "0"
    _run_with_inputs(monkeypatch, "9", "US", seller_id, "1", "", "0")
    out = capsys.readouterr().out
    assert "登録しました" in out
    assert "今後も取得しない" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"

    # known_seller_masterへは登録しないこと（意図的に独立させる、ユーザー指定）。
    with app_role_factory.get_session("US") as conn:
        seller_row = conn.execute(
            text("SELECT 1 FROM known_seller_master WHERE marketplace='US' AND seller_id=:sid"), {"sid": seller_id}
        ).fetchall()
        assert seller_row == []


def test_mark_storefront_blocked_menu_competitor_same_service_with_note(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "9", "JP", seller_id, "2", "統合ツール実行時に同業者エラー", "0")
    out = capsys.readouterr().out
    assert "登録しました" in out
    assert "同業者利用" in out

    with app_role_factory.get_session("JP") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "COMPETITOR_SAME_SERVICE"


def test_mark_storefront_blocked_menu_retry_later(monkeypatch, capsys, app_role_factory: SessionFactory):
    """メニュー9で新設のRETRY_LATER（3番目の選択肢）も選べること
    （ユーザー依頼、docs/DEVIATIONS.md #47参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    _run_with_inputs(monkeypatch, "9", "US", seller_id, "3", "在庫切れとのこと", "0")
    out = capsys.readouterr().out
    assert "登録しました" in out
    assert "一時的に取得できない" in out

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "RETRY_LATER"


def test_mark_storefront_blocked_menu_reports_already_registered(
    monkeypatch, capsys, app_role_factory: SessionFactory
):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note="低単価")

    _run_with_inputs(monkeypatch, "9", "US", seller_id, "2", "", "0")
    out = capsys.readouterr().out
    assert "既に登録済みです" in out
    assert "今後も取得しない" in out  # 元の理由が保持される（上書きされない）
    assert "メモ: 低単価" in out


def test_mark_storefront_blocked_menu_invalid_reason_choice_shows_message(monkeypatch, capsys):
    """3択（RETRY_LATER追加、docs/DEVIATIONS.md #47参照）になったため、無効な選択肢は
    "4"を使う。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    _run_with_inputs(monkeypatch, "9", "US", seller_id, "4", "0")
    out = capsys.readouterr().out
    assert "1〜3を入力してください。" in out


def test_sync_pending_menu_imports_when_file_found(
    monkeypatch, capsys, tmp_path, app_role_factory: SessionFactory
):
    """メニュー10: tool_output_asinが未取込のtool_run_idを検出し、ファイルが見つかれば
    自動取込すること（2026-09-16のユーザー確認、tool_intake.cli sync-pendingと共通実装）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
    asin = f"B{uuid.uuid4().hex[:9].upper()}"
    path = tmp_path / f"{tool_run_id}_output.csv"
    path.write_text(f"ASIN,取得日時\n{asin},2026-09-01 12:00:00\n", encoding="utf-8")

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

    # 他のテストが残した「未取込」のtool_execution_linkも対象に含まれ得るため
    # （DBはテストモジュール単位でしかリセットしない）、このtool_run_idにだけ
    # ファイルが見つかったことにする。
    monkeypatch.setattr(
        "tool_intake.pending_sync.find_tool_output_files",
        lambda mp, rid: [path] if rid == tool_run_id else [],
    )
    _run_with_inputs(monkeypatch, "10", "US", "0")

    out = capsys.readouterr().out
    assert f"tool_run_id={tool_run_id}" in out
    with app_role_factory.get_session("US") as conn:
        row = conn.execute(text("SELECT 1 FROM tool_output_asin WHERE asin=:asin"), {"asin": asin}).fetchall()
        assert len(row) == 1


def test_sync_pending_menu_reports_not_found_when_no_file(monkeypatch, capsys, app_role_factory: SessionFactory):
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
                "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) "
                "VALUES ('US', :sid, :rid, 'SELLER_PAGE')"
            ),
            {"sid": seller_id, "rid": tool_run_id},
        )

    monkeypatch.setattr("tool_intake.pending_sync.find_tool_output_files", lambda mp, rid: [])
    _run_with_inputs(monkeypatch, "10", "US", "0")

    out = capsys.readouterr().out
    assert "自動取込できた: 0件" in out
    assert f"tool_run_id={tool_run_id}" in out
