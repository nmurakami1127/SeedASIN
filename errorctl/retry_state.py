"""M12 リトライ状態管理: retry_attempt_counterの状態遷移を集約する。

mark_pending/mark_exhausted/resetは実装指示書Phase 2の指定メソッド。clearは
「再試行成功時は行削除」という設計（db/models.pyのコメント）に対応するため追加した
4本目のメソッド（ユーザー確認済み、2026-09-02。Phase 3のM4/B6実装時にこの想定で
問題ないか改めて確認すること）。

repository/base_repository.pyと同じ設計方針で、自分ではDBセッションを持たず、
呼び出し元が用意したConnectionを受け取って実行する（CLAUDE.md 7節「M4の3テーブル
更新は1トランザクション」の要件上、呼び出し元の既存トランザクションに参加できる
必要があるため）。commit/rollbackの責務は常に呼び出し元にある。

上限判定（attempt_count >= retry_config.max_retry_count）はこのクラスの責務ではなく、
呼び出し元（B6）が判断してmark_exhausted()を呼び出す（実装指示書「上限到達時のみ
本バッチがstatus='RETRY_EXHAUSTED'へ更新」）。
"""
from __future__ import annotations

from sqlalchemy import Connection, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import retry_attempt_counter


class RetryStateManager:
    def mark_pending(self, conn: Connection, account_id: str, marketplace: str, asin: str) -> int:
        """行が無ければattempt_count=1で新規作成、あれば+1してstatus='RETRY_PENDING'を保証する。

        戻り値は更新後のattempt_count（呼び出し元がretry_config.max_retry_countと比較して
        mark_exhaustedを呼ぶかどうかを判断するために使う）。
        """
        stmt = pg_insert(retry_attempt_counter).values(
            account_id=account_id,
            marketplace=marketplace,
            asin=asin,
            attempt_count=1,
            last_attempt_at=func.now(),
            status="RETRY_PENDING",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "marketplace", "asin"],
            set_={
                "attempt_count": retry_attempt_counter.c.attempt_count + 1,
                "last_attempt_at": func.now(),
                "status": "RETRY_PENDING",
            },
        ).returning(retry_attempt_counter.c.attempt_count)
        return conn.execute(stmt).scalar_one()

    def mark_exhausted(self, conn: Connection, account_id: str, marketplace: str, asin: str) -> None:
        """status='RETRY_EXHAUSTED'に更新する（attempt_countはそのまま）。

        B6（batch.b6_retry_runner）以外から呼ばないこと。
        """
        conn.execute(
            retry_attempt_counter.update()
            .where(
                retry_attempt_counter.c.account_id == account_id,
                retry_attempt_counter.c.marketplace == marketplace,
                retry_attempt_counter.c.asin == asin,
            )
            .values(status="RETRY_EXHAUSTED")
        )

    def reset(self, conn: Connection, account_id: str, marketplace: str, asin: str) -> None:
        """attempt_count=0, status='RETRY_PENDING'に更新する（RETRY_EXHAUSTED -> RETRY_PENDING）。

        /admin/retry/reissue またはCLI admin.retry_reissue 以外から呼ばないこと
        （CLAUDE.md 9節、この遷移の排他制御）。
        """
        conn.execute(
            retry_attempt_counter.update()
            .where(
                retry_attempt_counter.c.account_id == account_id,
                retry_attempt_counter.c.marketplace == marketplace,
                retry_attempt_counter.c.asin == asin,
            )
            .values(attempt_count=0, status="RETRY_PENDING")
        )

    def clear(self, conn: Connection, account_id: str, marketplace: str, asin: str) -> None:
        """再試行が成功した場合の行削除。retry_attempt_counterは永続履歴ではなく
        ワーキングテーブルであり、成功時は行ごと削除する設計（db/models.pyのコメント参照）。

        行が存在しない場合（そもそも一度も失敗していない対象）でも無害（0行DELETE）。
        M4のEligibilityChecker（初回成功時）・B6（再試行成功時）の両方から呼ばれる想定。
        """
        conn.execute(
            retry_attempt_counter.delete().where(
                retry_attempt_counter.c.account_id == account_id,
                retry_attempt_counter.c.marketplace == marketplace,
                retry_attempt_counter.c.asin == asin,
            )
        )
