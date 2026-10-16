"""統合ツールのストアフロントURLから取得できないセラーの記録（docs/DEVIATIONS.md #30）。

記録経路は3つある:
1. PERMANENTLY_EXCLUDED（自動/手動）: 今後も取得しない、という判断。理由は真贋リスク・
   低単価等さまざまで、詳細はnote（自由記述）に書く。tool_intake.cli ingest-logがログの
   ステータス「禁止セラー」を処理する際に自動記録するほか、対話式メニューcli.pyのメニュー1
   「2」・メニュー9からも選択できる（旧HIGH_RISK_COUNTRYをリネーム・意味を拡張したもの、
   docs/DEVIATIONS.md #41参照）。
2. COMPETITOR_SAME_SERVICE（手動）: 同業者利用。ASINはいずれ別手段（ASINリスト取込等）で
   取得できるため、上記とは区別する。seller_ledger.cli mark-storefront-blockedで人が
   気づいた時点で記録するほか、対話式メニューのメニュー1「2」・メニュー9からも選択できる。
3. RETRY_LATER（手動、2026-09-18追加）: 同業者利用かどうかは分からないが、何らかの
   一時的な理由で今は取得できず、いずれ再挑戦したい場合。PERMANENTLY_EXCLUDED（今後も
   取得しない）ともCOMPETITOR_SAME_SERVICE（同業者利用と分かっている）とも異なる、
   3つ目の独立した状態として追加した（ユーザー依頼、docs/DEVIATIONS.md #47参照）。
   対話式メニューのメニュー1「2」・メニュー9からのみ選択できる（mark-storefront-blocked
   コマンド自体はCOMPETITOR_SAME_SERVICE固定のまま変更していない）。

storefront_blocked_sellerはmarketplaceに依存しない情報のためRLS適用対象外
（db/table_names.NO_RLS_TABLES参照）。既存行は上書きしない（初回登録の理由・日付を
正とする）——mark_blocked()はON CONFLICT DO NOTHINGで判定し、既存行があればその内容を
そのまま返す（呼び出し元がエラーにせず状況を表示できるようにするため）。

誤った理由で登録してしまった場合の訂正はdelete_blocked()（物理削除）で行う
（seller_ledger.cli delete-storefront-block、docs/DEVIATIONS.md #42参照。対話式
メニューcli.pyのメニュー1からはこの削除を呼べない——#47でメニュー1の削除確認質問を
廃止したため、独立したコマンドとしてのみ引き続き使える）。削除後、必要なら正しい理由で
mark_blocked()を呼び直す。

4つ目の記録経路（自動、2026-10-07追加、docs/DEVIATIONS.md #54参照）:
mark_or_escalate_to_permanently_excluded()は「既存行は上書きしない」という上記の
原則に対する唯一の例外。asin_collector.check_recommendationsが実際のストアフロート
ページで商品0件（NO_PRODUCTS）と確認できたセラーについて呼び出す。
COMPETITOR_SAME_SERVICE・RETRY_LATER（＝いずれは取得する見込みがあるという人の
判断）で記録済みのセラーを、より確定的な「今後も取得しない」という自動判定結果で
PERMANENTLY_EXCLUDEDへ更新する。人が決めた保留理由を自動判定で上書きしてよいのは、
「ブラウザで実際に確認した結果、より確定的な判断（PERMANENTLY_EXCLUDED）に進む場合」
に限る——保留理由同士の付け替え（COMPETITOR_SAME_SERVICE⇄RETRY_LATER等）や、
PERMANENTLY_EXCLUDEDからの格下げは自動では行わない（CLAUDE.md参照）。
first_noted_at（最初に気づいた日）は変更しない——これは今回の自動判定の日ではなく、
人がそのセラーに最初に気づいた日という記録の意味を保つため。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import Connection, text

Reason = Literal["PERMANENTLY_EXCLUDED", "COMPETITOR_SAME_SERVICE", "RETRY_LATER"]

REASON_LABELS: dict[str, str] = {
    "PERMANENTLY_EXCLUDED": "今後も取得しない",
    "COMPETITOR_SAME_SERVICE": "同業者利用",
    "RETRY_LATER": "一時的に取得できない",
}


@dataclass
class BlockedSellerInfo:
    seller_id: str
    reason: str
    first_noted_at: date
    note: str | None = None

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


@dataclass
class MarkResult:
    status: Literal["NEW", "ALREADY_EXISTS"]
    info: BlockedSellerInfo


@dataclass
class EscalateResult:
    status: Literal["NEW", "UPGRADED", "ALREADY_EXCLUDED"]
    info: BlockedSellerInfo


class StorefrontBlockedSellerService:
    def mark_blocked(
        self,
        conn: Connection,
        seller_id: str,
        reason: Reason,
        seller_name: str | None = None,
        note: str | None = None,
        first_noted_at: date | None = None,
    ) -> MarkResult:
        """seller_idをstorefront_blocked_sellerへ記録する。既に記録済みなら既存の内容を
        そのまま返す（上書きしない、エラーにもしない）。

        first_noted_atを省略した場合はDBのDEFAULT（CURRENT_DATE）が使われる
        （通常の2経路はこちら）。scripts/import_legacy_seller_ledger.pyのような過去データ
        移行では、実際の初出日を明示的に渡せるようCOALESCEで対応する
        （docs/DEVIATIONS.md #31参照）。
        """
        inserted = conn.execute(
            text(
                """
                INSERT INTO storefront_blocked_seller (seller_id, seller_name, reason, note, first_noted_at)
                VALUES (:seller_id, :seller_name, :reason, :note, COALESCE(:first_noted_at, CURRENT_DATE))
                ON CONFLICT (seller_id) DO NOTHING
                RETURNING seller_id, reason, first_noted_at, note
                """
            ),
            {
                "seller_id": seller_id,
                "seller_name": seller_name,
                "reason": reason,
                "note": note,
                "first_noted_at": first_noted_at,
            },
        ).one_or_none()

        if inserted is not None:
            return MarkResult(
                status="NEW",
                info=BlockedSellerInfo(inserted.seller_id, inserted.reason, inserted.first_noted_at, inserted.note),
            )

        existing = conn.execute(
            text(
                "SELECT seller_id, reason, first_noted_at, note FROM storefront_blocked_seller "
                "WHERE seller_id = :seller_id"
            ),
            {"seller_id": seller_id},
        ).one()
        return MarkResult(
            status="ALREADY_EXISTS",
            info=BlockedSellerInfo(existing.seller_id, existing.reason, existing.first_noted_at, existing.note),
        )

    def mark_or_escalate_to_permanently_excluded(
        self,
        conn: Connection,
        seller_id: str,
        new_record_note: str,
    ) -> EscalateResult:
        """asin_collector.check_recommendationsが商品0件（NO_PRODUCTS）と確認した
        セラーを記録・更新する（ユーザー依頼、2026-10-07、docs/DEVIATIONS.md #54参照）。
        モジュールdocstring「4つ目の記録経路」参照。

        - 未登録のセラー: new_record_noteを添えて新規にPERMANENTLY_EXCLUDEDとして
          記録する（status="NEW"、mark_blocked()と同じ挙動）。
        - COMPETITOR_SAME_SERVICE・RETRY_LATERで記録済みのセラー: PERMANENTLY_EXCLUDED
          へ更新する（status="UPGRADED"）。noteの先頭に「自動判定: 商品0件（元の理由:
          <元の理由の表示ラベル>）」を追記し、元のnoteがあればその後ろに残す
          （ユーザー指定の書式）。first_noted_atは変更しない。
        - 既にPERMANENTLY_EXCLUDEDのセラー: 何もしない（status="ALREADY_EXCLUDED"）。
          recommendの候補はPERMANENTLY_EXCLUDEDを除外するため通常は起こらないが、
          手動編集されたCSVを入力にした場合等に備えた防御的分岐。
        """
        existing = self.get_blocked_info(conn, seller_id)
        if existing is None:
            result = self.mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note=new_record_note)
            return EscalateResult(status="NEW", info=result.info)

        if existing.reason == "PERMANENTLY_EXCLUDED":
            return EscalateResult(status="ALREADY_EXCLUDED", info=existing)

        prefix = f"自動判定: 商品0件（元の理由: {existing.reason_label}）"
        new_note = prefix if existing.note is None else f"{prefix}。{existing.note}"
        row = conn.execute(
            text(
                """
                UPDATE storefront_blocked_seller
                SET reason = 'PERMANENTLY_EXCLUDED', note = :note
                WHERE seller_id = :seller_id
                RETURNING seller_id, reason, first_noted_at, note
                """
            ),
            {"seller_id": seller_id, "note": new_note},
        ).one()
        return EscalateResult(
            status="UPGRADED",
            info=BlockedSellerInfo(row.seller_id, row.reason, row.first_noted_at, row.note),
        )

    def get_blocked_info(self, conn: Connection, seller_id: str) -> BlockedSellerInfo | None:
        row = conn.execute(
            text(
                "SELECT seller_id, reason, first_noted_at, note FROM storefront_blocked_seller "
                "WHERE seller_id = :seller_id"
            ),
            {"seller_id": seller_id},
        ).one_or_none()
        if row is None:
            return None
        return BlockedSellerInfo(row.seller_id, row.reason, row.first_noted_at, row.note)

    def delete_blocked(self, conn: Connection, seller_id: str) -> BlockedSellerInfo | None:
        """seller_idの記録を物理削除する。存在しなければNoneを返す（呼び出し元がエラーに
        せず「登録されていません」と表示できるようにするため、mark_blocked()と対称的な
        設計、ユーザー依頼、docs/DEVIATIONS.md #42参照）。誤った理由での登録を訂正する
        用途を想定している——訂正後は必要ならmark_blocked()で正しい理由を登録し直す。
        app_roleはstorefront_blocked_sellerに対しDELETE権限を既に持つ（alembic/versions/0008、
        追記専用テーブルではないためCLAUDE.md 3節の対象外）。
        """
        row = conn.execute(
            text(
                "DELETE FROM storefront_blocked_seller WHERE seller_id = :seller_id "
                "RETURNING seller_id, reason, first_noted_at, note"
            ),
            {"seller_id": seller_id},
        ).one_or_none()
        if row is None:
            return None
        return BlockedSellerInfo(row.seller_id, row.reason, row.first_noted_at, row.note)
