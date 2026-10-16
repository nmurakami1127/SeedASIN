"""SeedASINの日々の操作をまとめた対話式メニュー（ユーザー依頼、docs/DEVIATIONS.md #35参照）。

起動方法:
    python -m cli

既存の各CLIコマンド（seller_ledger.cli、tool_intake.cli、asin_collector.cli、
seller_performance.cli）のコマンド体系・画面出力は変えていない。このモジュールは
それらの裏にあるサービスクラスを直接呼び出す「まとめ役」であり、既存コマンドを
そのまま使う上級者向けの操作方法はそのまま残っている。ただし
tool_intake.cli import-tool-outputの--fileについては、対話式メニューと同じ
自動検出機能を追加している（省略可能になった以外、既存の使い方に影響は無い。
docs/DEVIATIONS.md #37参照）。

メニュー5（「継続的に使えるセラー」を確認する）は、
seller_performance.cli.write_recommendations_csv()をseller_performance.cli
recommendと共通利用している。表示項目・並び順の変更はSellerRecommenderService
自体の変更のため、両方の画面に同時に反映される（docs/DEVIATIONS.md #43参照）。
2026-10-07以降、結果は画面に一覧表示せずCSVへ出力し、画面には件数と出力先のみを
表示する（ユーザー依頼、docs/DEVIATIONS.md #53参照）。

メニュー3（統合ツールで取得したファイルを取り込む）は、marketplace・tool-run-idから
C:\\work\\input\\processed\\primary_check（JP）/ex_primary_check（US）配下の
「{tool-run-id}_...」形式のファイルを自動検出する（tool_intake.processed_file_lookup）。
1件見つかればそのまま使い、0件ならファイルパスの手入力にフォールバックし、複数件
見つかった場合は一覧から選ばせる。

各メニューの処理中に例外が発生しても、分かりやすいメッセージを表示してメインメニューへ
戻る（アプリ全体を落とさない。CLAUDE.md 6節のエラー分離の精神をCLI層にも適用）。

メニュー2（統合ツールのIDとひもづける）は、今回は既存のlink-tool-runをそのまま呼ぶだけの
実装にとどめている（marketplace・seller-id・tool-run-idを聞いて実行するのみ。以前は
発見経路（source）も聞いていたが、どのロジックからも参照されない自由入力で表記ゆれが
起きやすかったため2026-09-18に質問自体を廃止した。ユーザー依頼、docs/DEVIATIONS.md
#48参照。メニュー1「1. 統合ツールのIDとひもづける」も同じ_do_link_tool_run()を使うため
同時に反映される）。

_do_link_tool_run()は、ToolRunLinkService.record()がDuplicateToolRunErrorを送出した
場合のみ「既にtool_run_id=...が記録されています（スキップしました）。」と表示する
（2026-09-19修正、docs/DEVIATIONS.md #50参照）。以前は`except IntegrityError`で
無条件に同じメッセージを表示しており、tool_execution_link以外の想定外の
IntegrityErrorまで「既に記録済み」と誤って報告し得た（ユーザー報告のバグ調査により
判明。DuplicateToolRunError以外のIntegrityErrorは握りつぶさずそのまま伝播し、
呼び出し元のtry/exceptで「エラーが発生しました: ...」として表示される）。

メニュー1（取得履歴を見る）は、未登録セラーの場合も登録済みセラーの場合と全く同じ
3択（統合ツールのIDとひもづける／このセラーは取得できないと分かった／何もしない）を
提示する（ユーザー指摘、2026-09-11。docs/DEVIATIONS.md #40参照）。未登録の場合は
3択を提示する前にその場でSellerService.register()を実行するだけで、以降の表示・
選択肢は登録済みセラー（かつ取得履歴が無い状態）と完全に同じ扱いになる——以前は
未登録の場合だけ3択を出さずいきなり統合tool IDの入力を求めてしまう不具合があった。
「2. このセラーは取得できないと分かった」を選んだ場合、メニュー9と同じ_ask_block_reason()
でreasonを選ばせる（PERMANENTLY_EXCLUDED/COMPETITOR_SAME_SERVICE/RETRY_LATERの3択、
以前は2択（さらに以前はCOMPETITOR_SAME_SERVICE固定）だった、docs/DEVIATIONS.md
#41・#47参照）。

メニュー10（未取込ファイルをまとめて確認・取り込む）はtool_intake.pending_sync.PendingSyncService
をtool_intake.cli sync-pendingコマンドと共通利用する（ユーザー依頼、2026-09-16。
tool_execution_linkにはあるがtool_output_asinが未取込のtool_run_idを検出し、
processed_file_lookup.find_tool_output_files()で見つかったものだけ自動取込する。
詳細はtool_intake/pending_sync.pyのモジュールdocstring参照）。

禁止セラーの警告表示（blocked_infoがある場合）は、以前は続けて「このブロック記録を
削除しますか？(y/n)」を尋ね、yesならその場でStorefrontBlockedSellerService.delete_blocked()
を直接呼んで削除していたが、この確認は2026-09-18に廃止した（ユーザー依頼、
docs/DEVIATIONS.md #47参照）。現在は警告表示のすぐ後に、そのまま通常の（未登録/登録済み
の）3択の提示へ進む。誤った理由で登録してしまった場合の訂正は、引き続き独立したコマンド
seller_ledger.cli delete-storefront-block（seller_ledger.cli.confirm_and_delete_storefront_block()、
削除される理由・登録日を改めて表示してから確認する、docs/DEVIATIONS.md #42参照）で行う
——このメニューの流れからは呼ばれなくなったが、コマンド自体は変更していない。

メニュー7・8（入力ミス訂正用の削除）は、確認プロンプトを含む処理一式を
seller_ledger.cli.confirm_and_delete_seller()/confirm_and_delete_tool_run()として
共通化し、seller_ledger.cli delete-seller/delete-tool-runコマンドとこのメニューの
両方から同じ関数を呼ぶ（docs/DEVIATIONS.md #38参照）。tool_output_asinの削除には
admin_role接続が必要なため、この2メニューのみSessionFactory.for_admin_role()を
内部で生成する（メニュー4がReportSessionFactoryを内部生成するのと同じパターン）。

メニュー9（取得できないセラーとして記録する）は、メニュー1「2. このセラーは取得
できないと分かった」とは独立した入口（ユーザー依頼、docs/DEVIATIONS.md #39参照）。
統合ツールを一度も試していない段階（住所を見ただけで真贋リスクが高いと分かった場合等）
でも記録できるよう、メニュー1を経由させず、known_seller_masterへの新規登録も意図的に
行わない。reasonはPERMANENTLY_EXCLUDED（今後も取得しない。真贋リスク・低単価等、詳細は
noteに書く）/COMPETITOR_SAME_SERVICE（同業者利用）/RETRY_LATER（同業者かは不明だが
一時的な理由で今は取得できない、いずれ再挑戦する。2026-09-18追加、docs/DEVIATIONS.md
#47参照）の3択から選ばせる——この選択UI（_ask_block_reason()）はメニュー1「2」とも
共通化してあり、reasonの意味を整理し直した際（旧HIGH_RISK_COUNTRYをPERMANENTLY_EXCLUDED
へリネーム、RETRY_LATER追加）に両メニューとも同時に更新した（docs/DEVIATIONS.md
#41・#47参照）。結果の表示（新規/既存）は_mark_seller_blocked()として共通化してある。
既存のseller_ledger.cli mark-storefront-blockedコマンド自体（reason固定）は
変更していない。
"""
from __future__ import annotations

from pathlib import Path

from asin_collector.brand_page_collector import BrandPageCollectorService
from asin_collector.cli import export_collector_run, marketplace_from_url
from config.config_cache import ConfigCache
from db.session import ReportSessionFactory, SessionFactory
from seller_ledger.cli import confirm_and_delete_seller, confirm_and_delete_tool_run
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_ledger.tool_run_link import DuplicateToolRunError, ToolRunLinkService
from seller_performance.attribution import AttributionService
from seller_performance.cli import write_recommendations_csv
from seller_performance.seller_history import SellerHistoryService, format_history
from seller_performance.seller_recommender import SellerRecommenderService
from tool_intake.integration_tool_import import IntegrationToolImportService
from tool_intake.pending_sync import PendingSyncService
from tool_intake.processed_file_lookup import find_tool_output_files
from tool_intake.weekly_listing_import import WeeklyListingImportService


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _ask_marketplace(prompt: str = "marketplace (US/JP): ") -> str:
    while True:
        value = _ask(prompt).upper()
        if value in ("US", "JP"):
            return value
        print("US または JP を入力してください。")


def _ask_optional(prompt: str) -> str | None:
    value = _ask(prompt)
    return value or None


def _ask_yes_no(prompt: str) -> bool:
    while True:
        value = _ask(prompt).lower()
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("y または n を入力してください。")


def _print_import_summary(summary) -> None:
    print(
        f"import_run_id={summary.import_run_id} "
        f"total={summary.total_rows} success={summary.success_rows} error={summary.error_rows}"
    )


def _do_link_tool_run(app_factory: SessionFactory, marketplace: str, seller_id: str) -> None:
    tool_run_id = _ask("統合tool ID: ")

    try:
        with app_factory.get_session(marketplace) as conn, conn.begin():
            ToolRunLinkService().record(conn, marketplace, seller_id, tool_run_id)
    except DuplicateToolRunError:
        print(f"既にtool_run_id={tool_run_id}が記録されています（スキップしました）。")
        return

    print(f"linked: marketplace={marketplace} seller_id={seller_id} tool_run_id={tool_run_id}")


_BLOCK_REASON_CHOICES = {
    "1": "PERMANENTLY_EXCLUDED",
    "2": "COMPETITOR_SAME_SERVICE",
    "3": "RETRY_LATER",
}


def _ask_block_reason() -> str | None:
    """取得できない理由を選ばせる（メニュー1「2」・メニュー9で共通利用、
    docs/DEVIATIONS.md #41・#47参照）。不正な入力ならNoneを返す。"""
    print("取得できない理由を選択してください:")
    print("1. 今後も取得しない（真贋リスク・低単価等）")
    print("2. 同業者が利用している（いずれ別の方法で取得する）")
    print("3. その他の理由で今は取得できない（いずれ再挑戦する）")
    return _BLOCK_REASON_CHOICES.get(_ask("選択してください (1-3): "))


def _mark_seller_blocked(app_factory: SessionFactory, marketplace: str, seller_id: str, reason: str, note: str | None) -> None:
    """StorefrontBlockedSellerService.mark_blocked()を呼び、結果に応じたメッセージを表示する。
    メニュー1「2. このセラーは取得できないと分かった」とメニュー9（いずれも
    _ask_block_reason()でreasonを選ばせる、docs/DEVIATIONS.md #39・#41参照）の両方から
    共通利用する。"""
    with app_factory.get_session(marketplace) as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(conn, seller_id, reason, note=note)

    note_part = f"、メモ: {result.info.note}" if result.info.note else ""
    if result.status == "ALREADY_EXISTS":
        print(
            f"既に登録済みです（理由: {result.info.reason_label}、"
            f"登録日: {result.info.first_noted_at.isoformat()}{note_part}）"
        )
    else:
        print(f"登録しました: seller_id={seller_id} 理由: {result.info.reason_label}")


def _menu_history(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    seller_id = _ask("seller-id: ")

    with app_factory.get_session(marketplace) as conn:
        entries = SellerHistoryService().get_history(conn, marketplace, seller_id)
        blocked_info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
        is_registered = SellerService().exists(conn, marketplace, seller_id)

    if blocked_info is not None:
        # 禁止セラーであっても、警告表示だけで終了せず、続けて通常の取得履歴（0件でも）と
        # 下記の3択を提示する——storefront_blocked_sellerの理由に関わらず、独自プログラム
        # 等でASINを取得し統合ツールの「ASINリスト」機能経由で登録することは可能なため
        # （ユーザー確認済み、2026-09-10。docs/DEVIATIONS.md #36参照）。ブロック記録の
        # 削除確認はここでは行わない（2026-09-18廃止、docs/DEVIATIONS.md #47参照。訂正が
        # 必要な場合はseller_ledger.cli delete-storefront-blockを使う）。
        print(format_history(marketplace, seller_id, entries, blocked_info))
    else:
        if not is_registered:
            # 未登録セラーはここで先に登録だけ済ませ、その後は登録済みセラーと全く同じ
            # 表示・3択の流れに合流させる（未登録の場合だけ3択を出さずいきなり統合tool ID
            # の入力を求めていた不具合の修正、ユーザー指摘、docs/DEVIATIONS.md #40参照）。
            with app_factory.get_session(marketplace) as conn, conn.begin():
                SellerService().register(conn, marketplace, seller_id)
            print(f"seller_id={seller_id} はまだ登録されていなかったため、新規登録しました。")

        if entries:
            print(format_history(marketplace, seller_id, entries, blocked_info))
        else:
            print(f"seller_id={seller_id} は登録済みですが、取得履歴がまだありません。")

    print()
    print("1. 統合ツールのIDとひもづける")
    print("2. このセラーは取得できないと分かった")
    print("3. 何もしない")
    choice = _ask("選択してください (1-3): ")

    if choice == "1":
        _do_link_tool_run(app_factory, marketplace, seller_id)
    elif choice == "2":
        reason = _ask_block_reason()
        if reason is None:
            print("1〜3を入力してください。")
        else:
            note = _ask_optional("メモ（任意、Enterでスキップ）: ")
            _mark_seller_blocked(app_factory, marketplace, seller_id, reason, note)
    else:
        print("何もしません。")


def _menu_link_tool_run(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    seller_id = _ask("seller-id: ")
    _do_link_tool_run(app_factory, marketplace, seller_id)


def _choose_from_candidates(candidates: list[Path]) -> Path:
    print("複数のファイルが見つかりました。")
    for i, path in enumerate(candidates, start=1):
        print(f"{i}. {path}")
    while True:
        choice = _ask(f"選択してください (1-{len(candidates)}): ")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        print("番号が正しくありません。")


def _menu_import_tool_output(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    tool_run_id = _ask("統合tool ID: ")

    candidates = find_tool_output_files(marketplace, tool_run_id)
    if len(candidates) == 1:
        file_path = candidates[0]
        print(f"ファイルを自動検出しました: {file_path}")
    elif len(candidates) > 1:
        file_path = _choose_from_candidates(candidates)
    else:
        print("自動検出できませんでした。ファイルパスを入力してください。")
        file_path = Path(_ask("ファイルパス: "))

    summary = IntegrationToolImportService().import_file(app_factory, marketplace, tool_run_id, file_path)
    _print_import_summary(summary)


def _menu_collect_brand_page(app_factory: SessionFactory) -> None:
    target_url = _ask("target-url: ")
    marketplace = marketplace_from_url(target_url)
    result = BrandPageCollectorService().collect(app_factory, marketplace, target_url)
    print(f"collected {result.asin_count} ASIN(s) from {target_url} (marketplace={marketplace})")

    if _ask_yes_no("CSVとして出力しますか？(y/n): "):
        report_factory = ReportSessionFactory.for_kpi_report_role()
        output_path, count = export_collector_run(report_factory, result.run_id)
        print(f"{count}件を{output_path}に書き出しました")


def _menu_check_continuous_sellers(app_factory: SessionFactory) -> None:
    print("1. US")
    print("2. JP")
    print("3. 両方")
    choice = _ask("選択してください (1-3): ")
    marketplaces = {"1": ["US"], "2": ["JP"], "3": ["US", "JP"]}.get(choice)
    if marketplaces is None:
        print("1〜3のいずれかを入力してください。")
        return

    for marketplace in marketplaces:
        with app_factory.get_session(marketplace) as conn:
            count = AttributionService().attribute(conn, marketplace)
        print(f"attributed {count} new ASIN(s) for marketplace={marketplace}")

        config_row = ConfigCache(app_factory, marketplace).get("seller_recommendation_config")
        if config_row is None:
            print(
                f"seller_recommendation_configにmarketplace={marketplace}の行が無いため、"
                "recommendをスキップします。"
            )
            continue

        with app_factory.get_session(marketplace) as conn:
            recommendations = SellerRecommenderService().recommend(
                conn,
                marketplace,
                config_row["lookback_days"],
                config_row["min_days_since_last"],
            )
        output_path = write_recommendations_csv(recommendations, marketplace)
        print(
            f"marketplace={marketplace} lookback_days={config_row['lookback_days']} "
            f"min_days_since_last={config_row['min_days_since_last']} "
            f"recommendations={len(recommendations)}"
        )
        print(f"{len(recommendations)}件を{output_path}に書き出しました")


def _menu_import_weekly_listing(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    file_path = _ask("ファイルパス: ")
    summary = WeeklyListingImportService().import_file(app_factory, marketplace, Path(file_path))
    _print_import_summary(summary)


def _menu_delete_seller(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    seller_id = _ask("seller-id: ")
    admin_factory = SessionFactory.for_admin_role()
    confirm_and_delete_seller(app_factory, admin_factory, marketplace, seller_id)


def _menu_delete_tool_run(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    tool_run_id = _ask("統合tool ID: ")
    admin_factory = SessionFactory.for_admin_role()
    confirm_and_delete_tool_run(app_factory, admin_factory, marketplace, tool_run_id)


def _menu_mark_storefront_blocked(app_factory: SessionFactory) -> None:
    """メニュー1（取得履歴を見る）を経由せず、独立して「取得できないセラー」として
    記録する（ユーザー依頼、docs/DEVIATIONS.md #39参照）。統合ツールを一度も試して
    いない段階（住所を見ただけで真贋リスクが高いと分かった場合等）でも記録できるよう、
    known_seller_masterへの新規登録は意図的に行わない。"""
    marketplace = _ask_marketplace()
    seller_id = _ask("seller-id: ")

    reason = _ask_block_reason()
    if reason is None:
        print("1〜3を入力してください。")
        return

    note = _ask_optional("メモ（任意、Enterでスキップ）: ")
    _mark_seller_blocked(app_factory, marketplace, seller_id, reason, note)


def _menu_sync_pending(app_factory: SessionFactory) -> None:
    marketplace = _ask_marketplace()
    result = PendingSyncService().sync(app_factory, marketplace)
    print(result.format())


_MENU_ITEMS = [
    ("1", "セラー/ブランド/キーワードの取得履歴を見る", _menu_history),
    ("2", "統合ツールのIDとセラー/ブランド/キーワードをひもづける", _menu_link_tool_run),
    ("3", "統合ツールで取得したファイルを取り込む", _menu_import_tool_output),
    ("4", "ブランド・ランキングページからASINを集める", _menu_collect_brand_page),
    ("5", "「継続的に使えるセラー」を確認する", _menu_check_continuous_sellers),
    ("6", "出品完了リストを取り込む", _menu_import_weekly_listing),
    ("7", "セラーの記録を削除する", _menu_delete_seller),
    ("8", "紐づけの記録を削除する", _menu_delete_tool_run),
    ("9", "取得できないセラーとして記録する", _menu_mark_storefront_blocked),
    ("10", "未取込ファイルをまとめて確認・取り込む", _menu_sync_pending),
]


def main() -> None:
    app_factory = SessionFactory.for_app_role()
    try:
        while True:
            print("\n何をしますか？")
            for key, label, _ in _MENU_ITEMS:
                print(f"{key}. {label}")
            print("0. 終了")

            choice = _ask("選択してください: ")
            if choice == "0":
                print("終了します。")
                return

            action = next((fn for key, _, fn in _MENU_ITEMS if key == choice), None)
            if action is None:
                print("該当する番号がありません。")
                continue

            try:
                action(app_factory)
            except SystemExit as exc:
                print(f"エラー: {exc}")
            except Exception as exc:  # 1メニューのエラーでアプリ全体を落とさない
                print(f"エラーが発生しました: {exc}")
    except (EOFError, KeyboardInterrupt):
        print("\n終了します。")


if __name__ == "__main__":
    main()
