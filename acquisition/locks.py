"""共通ロック。出典の判定を変えうる全ての操作（claim・保留/除外/解除・間隔変更・自動保留・
旧証拠の分類/確定/却下）は、同じ「出典の識別ロック」に参加して直列化する。

- 識別ロック: (取得先市場, 出典種別, key_hash)から作るトランザクション・アドバイザリロック。
  出典の行がまだ無い段階の旧証拠の判断でも、同じキーで取れる。
- ロック順序（デッドロック回避）: 識別ロック → acquisition_source行(FOR UPDATE) → collector_run行(FOR UPDATE)。
  heartbeat・ASIN追記は run 行のロックだけを取る（上位のロックを後から取らない）。
- 旧証拠の判断（複数区画をまたぐ）は、証拠グループ単位のセッション・アドバイザリロックを使う。

保証範囲: これらのロックに参加するのは本パッケージの操作だけ。既存のstorefront_blocked_seller
の更新経路（seller_ledger.storefront_blocked等）は参加しない。そのためclaimは「判定クエリ実行時点で
コミット済みのstorefront_blocked_sellerの内容」を反映するが、claim処理中に並行してコミットされた
更新は反映されない可能性がある（競合の完全防止は保証しない）。
"""
from __future__ import annotations

from sqlalchemy import Connection, text


def identity_lock(conn: Connection, marketplace: str, source_type: str, key_hash: str) -> None:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"acq-identity:{marketplace}:{source_type}:{key_hash}"},
    )


def group_lock_key(evidence_group: str) -> str:
    return f"acq-legacy-group:{evidence_group}"
