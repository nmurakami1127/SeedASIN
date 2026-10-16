"""seller_ledger CLIエントリポイント。

registerに発見経路（--source）は無い。どのロジックからも参照されない記録専用の値
だったため削除した（ユーザー確認済み、2026-09-06。docs/DEVIATIONS.md #25参照）。
link-tool-runの--source（tool_execution_link.source）も同じ理由（どのロジックからも
参照されない自由入力の値で、表記ゆれが起きやすい）で2026-09-18に廃止した
（ユーザー依頼、docs/DEVIATIONS.md #48参照）。列自体は既存データ保持のため残し、
NULL許容にしただけ——tool_intake.cli ingest-logが統合ツールのログHTMLから自動記録する
用途では引き続き使われている。

link-tool-runで同一(marketplace, tool_run_id)を再度実行した場合、
ToolRunLinkService.DuplicateToolRunErrorを捕捉し「既にtool_run_id=...が記録されて
います（スキップしました）。」と表示して正常終了する（対話式メニューcli.pyの
_do_link_tool_run()と同じメッセージ）。2026-09-19以前はこの例外処理自体が無く、
標準コマンド単体では生のIntegrityErrorのトレースバックでクラッシュしていた
（ユーザー報告のバグ調査中に発見、docs/DEVIATIONS.md #50参照）。

mark-storefront-blockedは、統合ツールの実行時に「同業者利用」等の理由でエラーになると
人が気づいた時点で手動記録するためのコマンド（reasonは常にCOMPETITOR_SAME_SERVICEに
固定、引数での指定は無い）。他の2つの記録経路（PERMANENTLY_EXCLUDED。今後も取得しない、
という意味。旧HIGH_RISK_COUNTRYをリネーム・意味を拡張したもの、docs/DEVIATIONS.md #41
参照／RETRY_LATER。一時的な理由で今は取得できないが、いずれ再挑戦する、という意味。
docs/DEVIATIONS.md #47参照）は、tool_intake.cli ingest-logが自動で行う
PERMANENTLY_EXCLUDEDの記録を除き、対話式メニューcli.pyのメニュー1・9からのみ選択でき、
このコマンド自体には引数が無い（docs/DEVIATIONS.md #30参照）。storefront_blocked_seller
はmarketplaceに依存しない情報のため、このコマンドに--marketplaceは無い。

delete-seller/delete-tool-runは、セラーID・marketplace・tool-run-idの入力ミスに
気づいた際の訂正用コマンド（ユーザー依頼、docs/DEVIATIONS.md #38参照）。物理削除
（DELETE文）を行う破壊的操作のため、削除される件数を表示した上でy/nの確認を必ず取る。
delete-tool-runはknown_seller_masterを削除しない（セラー自体は残す）。
tool_output_asin（追記専用テーブル）の削除にはadmin_role接続を使う——app_roleには
引き続きDELETE権限が無い（CLAUDE.md 3節、alembic/versions/0010で例外的にadmin_roleへ
限定付与）。delete-seller/delete-tool-runは意図的にstorefront_blocked_sellerを削除
対象に含めない（独立した事実のため、docs/DEVIATIONS.md #38参照）。

delete-storefront-blockは、storefront_blocked_sellerの誤登録（例: 本来
PERMANENTLY_EXCLUDEDのはずがCOMPETITOR_SAME_SERVICEで登録されていた等）を訂正する
ための専用コマンド（ユーザー依頼、docs/DEVIATIONS.md #42参照）。storefront_blocked_seller
はmarketplaceに依存しないテーブルだが、コマンド体系の一貫性のため--marketplaceを
受け取る（実際の検索・削除はseller_idのみで行う。SessionFactory.get_session()の
接続確立に使うだけで、削除対象の絞り込みには使わない）。storefront_blocked_sellerは
追記専用テーブルではなくapp_roleが既にDELETE権限を持つため（alembic/versions/0008）、
delete-seller/delete-tool-runと異なりadmin_role接続は不要。

実行例:
    python -m seller_ledger.cli register --marketplace=US --seller-id=A1B2C3
    python -m seller_ledger.cli link-tool-run --marketplace=US --seller-id=A1B2C3 \\
        --tool-run-id=7128337
    python -m seller_ledger.cli mark-storefront-blocked --seller-id=A1B2C3 \\
        --seller-name="Example Store" --note="統合ツール実行時に同業者エラー"
    python -m seller_ledger.cli delete-seller --marketplace=US --seller-id=A1B2C3
    python -m seller_ledger.cli delete-tool-run --marketplace=US --tool-run-id=7128337
    python -m seller_ledger.cli delete-storefront-block --marketplace=US --seller-id=A1B2C3
"""
from __future__ import annotations

import argparse

from db.session import SessionFactory
from seller_ledger.deletion import SellerDeletionService, ToolRunDeletionService
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_ledger.tool_run_link import DuplicateToolRunError, ToolRunLinkService

# storefront_blocked_sellerはmarketplaceに依存しないテーブルだが、SessionFactory.get_session()
# はUS/JPのいずれかを要求する（CLAUDE.md 2節、接続確立時に必ずSETする方針）。このテーブルには
# RLSポリシー自体が存在しないため値そのものに意味は無く、接続を開くための形式的な指定である。
_STOREFRONT_BLOCKED_CONNECTION_MARKETPLACE = "US"


def _cmd_register(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    with factory.get_session(args.marketplace) as conn, conn.begin():
        result = SellerService().register(conn, args.marketplace, args.seller_id, args.source_asin)
    print(f"{result}: marketplace={args.marketplace} seller_id={args.seller_id}")


def _cmd_link_tool_run(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    try:
        with factory.get_session(args.marketplace) as conn, conn.begin():
            ToolRunLinkService().record(conn, args.marketplace, args.seller_id, args.tool_run_id)
    except DuplicateToolRunError:
        print(f"既にtool_run_id={args.tool_run_id}が記録されています（スキップしました）。")
        return
    print(
        f"linked: marketplace={args.marketplace} seller_id={args.seller_id} "
        f"tool_run_id={args.tool_run_id}"
    )


def _cmd_mark_storefront_blocked(args: argparse.Namespace) -> None:
    factory = SessionFactory.for_app_role()
    with factory.get_session(_STOREFRONT_BLOCKED_CONNECTION_MARKETPLACE) as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(
            conn, args.seller_id, "COMPETITOR_SAME_SERVICE", args.seller_name, args.note
        )

    note_part = f"、メモ: {result.info.note}" if result.info.note else ""
    if result.status == "ALREADY_EXISTS":
        print(
            f"既に登録済みです（理由: {result.info.reason_label}、"
            f"初回登録日: {result.info.first_noted_at.isoformat()}{note_part}）"
        )
    else:
        print(f"登録しました: seller_id={args.seller_id} 理由: {result.info.reason_label}")


def _ask_yes_no(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def confirm_and_delete_seller(
    app_factory: SessionFactory, admin_factory: SessionFactory, marketplace: str, seller_id: str
) -> None:
    """件数を表示してy/nの確認を取ってから削除する。cli.py（対話式メニュー、
    docs/DEVIATIONS.md #38参照）の「7」からも同じ関数を呼び、確認フローを共通化する。"""
    with app_factory.get_session(marketplace) as conn:
        counts = SellerDeletionService().count(conn, marketplace, seller_id)

    if counts.total == 0:
        print("該当するデータがありません。")
        return

    print(
        "本当に削除しますか？削除される件数: "
        f"known_seller_master {counts.known_seller_master}件、"
        f"tool_execution_link {counts.tool_execution_link}件、"
        f"tool_output_asin {counts.tool_output_asin}件、"
        f"seller_attribution {counts.seller_attribution}件"
    )
    if not _ask_yes_no("y/n: "):
        print("削除を中止しました。")
        return

    with app_factory.get_session(marketplace) as app_conn, admin_factory.get_session(marketplace) as admin_conn:
        deleted = SellerDeletionService().delete(app_conn, admin_conn, marketplace, seller_id)

    print(
        "削除しました: "
        f"known_seller_master {deleted.known_seller_master}件、"
        f"tool_execution_link {deleted.tool_execution_link}件、"
        f"tool_output_asin {deleted.tool_output_asin}件、"
        f"seller_attribution {deleted.seller_attribution}件"
    )


def confirm_and_delete_tool_run(
    app_factory: SessionFactory, admin_factory: SessionFactory, marketplace: str, tool_run_id: str
) -> None:
    with app_factory.get_session(marketplace) as conn:
        counts = ToolRunDeletionService().count(conn, marketplace, tool_run_id)

    if counts.total == 0:
        print("該当するデータがありません。")
        return

    print(
        "本当に削除しますか？削除される件数: "
        f"tool_execution_link {counts.tool_execution_link}件、"
        f"tool_output_asin {counts.tool_output_asin}件、"
        f"seller_attribution {counts.seller_attribution}件"
    )
    if not _ask_yes_no("y/n: "):
        print("削除を中止しました。")
        return

    with app_factory.get_session(marketplace) as app_conn, admin_factory.get_session(marketplace) as admin_conn:
        deleted = ToolRunDeletionService().delete(app_conn, admin_conn, marketplace, tool_run_id)

    print(
        "削除しました: "
        f"tool_execution_link {deleted.tool_execution_link}件、"
        f"tool_output_asin {deleted.tool_output_asin}件、"
        f"seller_attribution {deleted.seller_attribution}件"
    )


def confirm_and_delete_storefront_block(
    app_factory: SessionFactory, marketplace: str, seller_id: str
) -> None:
    """storefront_blocked_sellerの誤登録を訂正するための削除（ユーザー依頼、
    docs/DEVIATIONS.md #42参照）。marketplaceはこのテーブル自体には意味を持たないが、
    SessionFactory.get_session()の接続確立とコマンド体系の一貫性のために受け取る。
    未登録ならエラーにせず「登録されていません」と表示するだけで終える。"""
    with app_factory.get_session(marketplace) as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    if info is None:
        print(f"seller_id={seller_id} は登録されていません。")
        return

    note_part = f"、メモ: {info.note}" if info.note else ""
    print(
        f"本当に削除しますか？（理由: {info.reason_label}、"
        f"登録日: {info.first_noted_at.isoformat()}{note_part}）"
    )
    if not _ask_yes_no("y/n: "):
        print("削除を中止しました。")
        return

    with app_factory.get_session(marketplace) as conn, conn.begin():
        StorefrontBlockedSellerService().delete_blocked(conn, seller_id)

    print(f"削除しました: seller_id={seller_id}")


def _cmd_delete_seller(args: argparse.Namespace) -> None:
    app_factory = SessionFactory.for_app_role()
    admin_factory = SessionFactory.for_admin_role()
    confirm_and_delete_seller(app_factory, admin_factory, args.marketplace, args.seller_id)


def _cmd_delete_tool_run(args: argparse.Namespace) -> None:
    app_factory = SessionFactory.for_app_role()
    admin_factory = SessionFactory.for_admin_role()
    confirm_and_delete_tool_run(app_factory, admin_factory, args.marketplace, args.tool_run_id)


def _cmd_delete_storefront_block(args: argparse.Namespace) -> None:
    app_factory = SessionFactory.for_app_role()
    confirm_and_delete_storefront_block(app_factory, args.marketplace, args.seller_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m seller_ledger.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_register = subparsers.add_parser("register", help="セラーを台帳へ登録する（既存なら再確認扱い）")
    p_register.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_register.add_argument("--seller-id", required=True)
    p_register.add_argument("--source-asin", default=None, help="起点となった商品のASIN（任意）")
    p_register.set_defaults(func=_cmd_register)

    p_link = subparsers.add_parser("link-tool-run", help="統合ツール実行IDとセラーの対応を記録する")
    p_link.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_link.add_argument("--seller-id", required=True)
    p_link.add_argument("--tool-run-id", required=True, help="統合ツール出力ファイル名から抽出した実行ID")
    p_link.set_defaults(func=_cmd_link_tool_run)

    p_blocked = subparsers.add_parser(
        "mark-storefront-blocked",
        help="統合ツールの同業者利用エラー等で気づいたセラーを手動記録する"
        "（reasonはCOMPETITOR_SAME_SERVICEに固定）",
    )
    p_blocked.add_argument("--seller-id", required=True)
    p_blocked.add_argument("--seller-name", default=None)
    p_blocked.add_argument("--note", default=None)
    p_blocked.set_defaults(func=_cmd_mark_storefront_blocked)

    p_del_seller = subparsers.add_parser(
        "delete-seller",
        help="入力ミス訂正用: セラーの記録をknown_seller_master/tool_execution_link/"
        "tool_output_asin/seller_attributionから削除する（y/n確認あり）",
    )
    p_del_seller.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_del_seller.add_argument("--seller-id", required=True)
    p_del_seller.set_defaults(func=_cmd_delete_seller)

    p_del_run = subparsers.add_parser(
        "delete-tool-run",
        help="入力ミス訂正用: 統合ツール実行の記録をtool_execution_link/tool_output_asin/"
        "seller_attributionから削除する（known_seller_masterは残す、y/n確認あり）",
    )
    p_del_run.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_del_run.add_argument("--tool-run-id", required=True)
    p_del_run.set_defaults(func=_cmd_delete_tool_run)

    p_del_block = subparsers.add_parser(
        "delete-storefront-block",
        help="入力ミス訂正用: storefront_blocked_sellerの記録を削除する（y/n確認あり）",
    )
    p_del_block.add_argument("--marketplace", required=True, choices=["US", "JP"])
    p_del_block.add_argument("--seller-id", required=True)
    p_del_block.set_defaults(func=_cmd_delete_storefront_block)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
