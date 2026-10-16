"""セラー台帳: 人が見つけたセラーをknown_seller_masterへ登録し、既出件数（NEW/EXISTING）を
自動判定する（docs/phase3_implementation_guide.md 3節）。

旧M8のNEW/EXISTING判定は`first_known_at`と`seller_checked_at`（Seller確認イベント単位、
seller_check_detail経由）を比較していたが、その比較対象だったseller_check/seller_check_detail
は廃止済み（docs/DEVIATIONS.md #13）。新スコープでは「人がセラーを見つけて台帳に登録する」
という行為そのものが唯一のイベントであり、known_seller_masterに該当行が存在するかどうかだけで
NEW/EXISTINGを判定する。

register()はかつて発見経路（discovery_source、known_seller_master.first_known_source列）を
受け取っていたが、どのロジックからも参照されない記録専用の値だったため削除した
（ユーザー確認済み、2026-09-06。docs/DEVIATIONS.md #25参照。列自体もマイグレーション
0006で削除済み）。将来必要になれば改めて設計し直す。
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy import Connection, text

Judgement = Literal["NEW", "EXISTING"]


class SellerService:
    def exists(self, conn: Connection, marketplace: str, seller_id: str) -> bool:
        """known_seller_masterに既に登録済みかどうかを調べる（読み取り専用）。
        cli.py（対話式メニュー、docs/DEVIATIONS.md #35参照）が、履歴表示時に
        「未登録セラー」を判定するために使う。"""
        row = conn.execute(
            text(
                "SELECT 1 FROM known_seller_master WHERE marketplace = :marketplace AND seller_id = :seller_id"
            ),
            {"marketplace": marketplace, "seller_id": seller_id},
        ).fetchone()
        return row is not None

    def register(
        self,
        conn: Connection,
        marketplace: str,
        seller_id: str,
        source_asin: str | None = None,
    ) -> Judgement:
        """セラーをknown_seller_masterへ登録する（無ければ新規作成、あれば再確認扱い）。

        source_asinは起点となった商品のASIN（任意、無ければNone）。

        既存行がある場合はlast_seen_atのみ更新し、first_known_at/known_at_reliable/
        known_before_seedasin/source_asinは初回登録時の値を保持する
        （初回登録後は更新不可という原則。CLAUDE.md）。latest_product_count/
        latest_checked_atはここでは一切更新しない（統合ツール出力取込後にtool_intakeが
        担う別イベントのため。docs/DEVIATIONS.md参照）。

        INSERT ... ON CONFLICT ... RETURNING (xmax = 0)で、SELECTしてからINSERT/UPDATEを
        分岐する2ステップ方式より競合安全にNEW/EXISTINGを判定する
        （xmax = 0はこの操作で実際にINSERTされた行であることを示すPostgreSQLの慣用句）。
        """
        row = conn.execute(
            text(
                """
                INSERT INTO known_seller_master
                    (marketplace, seller_id, first_known_at,
                     known_at_reliable, known_before_seedasin, source_asin, last_seen_at)
                VALUES
                    (:marketplace, :seller_id, now(),
                     true, false, :source_asin, now())
                ON CONFLICT (marketplace, seller_id) DO UPDATE
                    SET last_seen_at = now()
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "marketplace": marketplace,
                "seller_id": seller_id,
                "source_asin": source_asin,
            },
        ).one()
        return "NEW" if row.inserted else "EXISTING"
