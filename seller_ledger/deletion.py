"""入力ミス訂正のための削除サービス（ユーザー依頼、docs/DEVIATIONS.md #38参照）。

セラーID・marketplace・tool-run-idの入力ミスに気づいた際に、間違った記録を取り消す
ための物理削除（DELETE文）を行う。誤って使い続けられることを防ぐため、これらの
テーブルはCLAUDE.mdの追記専用テーブル（3節）には該当しない——ただし例外が1つある:
`tool_output_asin`は追記専用テーブルであり、`app_role`にはDELETE権限が無い
（`REVOKE UPDATE, DELETE`）。そのため、tool_output_asinの削除のみ`admin_role`接続で
行う（ユーザー承認済み、2026-09-11。admin_roleへの限定的なDELETE権限付与は
alembic/versions/0010参照）。app_role自体のtool_output_asinに対する権限は変更していない。

**注意（2つのロールをまたぐため完全な原子性は無い）**: tool_output_asin（admin_role
接続）と、その他のテーブル（app_role接続）は別々のDB接続・別々のトランザクションで
処理される。異なるロールの接続を1つのトランザクションにまとめることはできないため。
削除は必ず件数を人に確認させてから実行するため、確認後に片方だけ失敗する可能性は
低いが、完全な原子性が無い点は設計上の制約として明記しておく。

削除順序: tool_output_asin（admin_role、他方に依存されないよう先に消す）→
seller_attribution → tool_execution_link → （delete-sellerのみ）known_seller_master。
tool_output_asinをtool_execution_link経由で特定する場合（delete-seller）は、
tool_execution_linkが消える前に済ませる必要があるため、この順序が必須。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text


@dataclass
class SellerDeletionCounts:
    known_seller_master: int = 0
    tool_execution_link: int = 0
    tool_output_asin: int = 0
    seller_attribution: int = 0

    @property
    def total(self) -> int:
        return self.known_seller_master + self.tool_execution_link + self.tool_output_asin + self.seller_attribution


@dataclass
class ToolRunDeletionCounts:
    tool_execution_link: int = 0
    tool_output_asin: int = 0
    seller_attribution: int = 0

    @property
    def total(self) -> int:
        return self.tool_execution_link + self.tool_output_asin + self.seller_attribution


class SellerDeletionService:
    def count(self, conn: Connection, marketplace: str, seller_id: str) -> SellerDeletionCounts:
        params = {"mp": marketplace, "sid": seller_id}
        known = conn.execute(
            text("SELECT count(*) FROM known_seller_master WHERE marketplace=:mp AND seller_id=:sid"), params
        ).scalar_one()
        links = conn.execute(
            text("SELECT count(*) FROM tool_execution_link WHERE marketplace=:mp AND seller_id=:sid"), params
        ).scalar_one()
        asins = conn.execute(
            text(
                "SELECT count(*) FROM tool_output_asin WHERE marketplace=:mp AND tool_run_id IN "
                "(SELECT tool_run_id FROM tool_execution_link WHERE marketplace=:mp AND seller_id=:sid)"
            ),
            params,
        ).scalar_one()
        attributions = conn.execute(
            text("SELECT count(*) FROM seller_attribution WHERE marketplace=:mp AND seller_id=:sid"), params
        ).scalar_one()
        return SellerDeletionCounts(
            known_seller_master=known, tool_execution_link=links, tool_output_asin=asins, seller_attribution=attributions
        )

    def delete(
        self, app_conn: Connection, admin_conn: Connection, marketplace: str, seller_id: str
    ) -> SellerDeletionCounts:
        params = {"mp": marketplace, "sid": seller_id}

        # admin_roleはtool_execution_linkへのアクセス権を持たない（tool_output_asinのみに
        # 限定したGRANT、alembic/versions/0010）ため、対象tool_run_idの一覧は先に
        # app_role接続で取得し、admin_role側へは具体的な値のリストとして渡す
        # （admin_role側でtool_execution_linkをSELECTするサブクエリは書けない）。
        # tool_execution_linkが消える前に、経由するtool_output_asinを先に削除する。
        tool_run_ids = app_conn.execute(
            text("SELECT tool_run_id FROM tool_execution_link WHERE marketplace=:mp AND seller_id=:sid"), params
        ).scalars().all()
        # execute()はSQLAlchemy 2.xのautobeginでトランザクションを開始してしまうため、
        # 後続のwith app_conn.begin():が使えるよう先に閉じておく（db/session.pyの
        # get_session()と同じ理由）。
        app_conn.commit()

        asin_rowcount = 0
        if tool_run_ids:
            with admin_conn.begin():
                asin_result = admin_conn.execute(
                    text("DELETE FROM tool_output_asin WHERE marketplace=:mp AND tool_run_id = ANY(:rids)"),
                    {"mp": marketplace, "rids": list(tool_run_ids)},
                )
                asin_rowcount = asin_result.rowcount

        with app_conn.begin():
            attribution_result = app_conn.execute(
                text("DELETE FROM seller_attribution WHERE marketplace=:mp AND seller_id=:sid"), params
            )
            link_result = app_conn.execute(
                text("DELETE FROM tool_execution_link WHERE marketplace=:mp AND seller_id=:sid"), params
            )
            seller_result = app_conn.execute(
                text("DELETE FROM known_seller_master WHERE marketplace=:mp AND seller_id=:sid"), params
            )

        return SellerDeletionCounts(
            known_seller_master=seller_result.rowcount,
            tool_execution_link=link_result.rowcount,
            tool_output_asin=asin_rowcount,
            seller_attribution=attribution_result.rowcount,
        )


class ToolRunDeletionService:
    def count(self, conn: Connection, marketplace: str, tool_run_id: str) -> ToolRunDeletionCounts:
        params = {"mp": marketplace, "rid": tool_run_id}
        links = conn.execute(
            text("SELECT count(*) FROM tool_execution_link WHERE marketplace=:mp AND tool_run_id=:rid"), params
        ).scalar_one()
        asins = conn.execute(
            text("SELECT count(*) FROM tool_output_asin WHERE marketplace=:mp AND tool_run_id=:rid"), params
        ).scalar_one()
        attributions = conn.execute(
            text(
                "SELECT count(*) FROM seller_attribution WHERE marketplace=:mp AND first_seen_via_tool_run_id=:rid"
            ),
            params,
        ).scalar_one()
        return ToolRunDeletionCounts(tool_execution_link=links, tool_output_asin=asins, seller_attribution=attributions)

    def delete(
        self, app_conn: Connection, admin_conn: Connection, marketplace: str, tool_run_id: str
    ) -> ToolRunDeletionCounts:
        params = {"mp": marketplace, "rid": tool_run_id}

        with admin_conn.begin():
            asin_result = admin_conn.execute(
                text("DELETE FROM tool_output_asin WHERE marketplace=:mp AND tool_run_id=:rid"), params
            )

        with app_conn.begin():
            # first_seen_via_tool_run_idが指すtool_run_idが消えると、attributeが二度と
            # 再評価しない（ON CONFLICT DO NOTHING）ため誤った帰属が残り続けてしまう。
            # 訂正の趣旨に合わせ、このtool_run_id由来のseller_attribution行も削除する
            # （ユーザー承認済み、2026-09-11）。
            attribution_result = app_conn.execute(
                text("DELETE FROM seller_attribution WHERE marketplace=:mp AND first_seen_via_tool_run_id=:rid"),
                params,
            )
            link_result = app_conn.execute(
                text("DELETE FROM tool_execution_link WHERE marketplace=:mp AND tool_run_id=:rid"), params
            )

        return ToolRunDeletionCounts(
            tool_execution_link=link_result.rowcount,
            tool_output_asin=asin_result.rowcount,
            seller_attribution=attribution_result.rowcount,
        )
