"""import_run_history / import_error_log への記帳を共通化する。

旧実装指示書のB1/B2パターン（CSV1行ごとに独立トランザクションでUPSERT、
行番号・エラー内容をimport_error_logへ記録し処理継続、最終件数サマリを
import_run_historyへ記録）をtool_intakeの2つの取込サービスで共通利用する。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection, text


@dataclass
class ImportSummary:
    import_run_id: str
    total_rows: int
    success_rows: int
    error_rows: int


class ImportRunRecorder:
    """1回の取込実行（1ファイル）を通じて呼び出す。呼び出し元は行ごとに
    `with conn.begin():` で独立トランザクションを区切ること
    （1行の失敗が他行の取込を止めないため）。"""

    def __init__(self, conn: Connection, marketplace: str, import_type: str, source_file_name: str) -> None:
        self._conn = conn
        self._marketplace = marketplace
        # 旧実装指示書の採番規則{marketplace}-{import_type}-{yyyyMMddHHmmss}は秒単位までしか
        # 持たず、同一marketplace×import_typeの取込が同一秒内に2回走ると
        # import_run_history_pkeyの一意制約に衝突する（結合テストで実際に再現）。
        # 4桁のランダムhexサフィックスを付与して衝突を防ぐ。
        suffix = uuid.uuid4().hex[:4]
        self.import_run_id = f"{marketplace}-{import_type}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{suffix}"
        self._total = 0
        self._success = 0
        self._errors = 0
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO import_run_history "
                    "(import_run_id, marketplace, import_type, source_file_name, executed_at, status) "
                    "VALUES (:run_id, :mp, :itype, :fname, now(), 'RUNNING')"
                ),
                {
                    "run_id": self.import_run_id,
                    "mp": marketplace,
                    "itype": import_type,
                    "fname": source_file_name,
                },
            )

    def record_success(self) -> None:
        self._total += 1
        self._success += 1

    def record_skip(self) -> None:
        """エラーではないが取込対象外だった行（例: SKU未設定行）。total_rowsには含めるが
        success_rows/error_rowsのどちらにも計上しない。"""
        self._total += 1

    def record_error(self, row_number: int, raw_row_text: str, error_code: str, error_message: str) -> None:
        self._total += 1
        self._errors += 1
        with self._conn.begin():
            self._conn.execute(
                text(
                    "INSERT INTO import_error_log "
                    "(import_run_id, marketplace, row_number, raw_row_text, error_code, error_message) "
                    "VALUES (:run_id, :mp, :row_number, :raw_row_text, :error_code, :error_message)"
                ),
                {
                    "run_id": self.import_run_id,
                    "mp": self._marketplace,
                    "row_number": row_number,
                    "raw_row_text": raw_row_text,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )

    def mark_failed(self) -> None:
        """取込全体が致命的エラーで中断した場合に、実行履歴をFAILEDにする（行単位の
        エラーはrecord_error()で記録済みの件数のまま残す）。"""
        with self._conn.begin():
            self._conn.execute(
                text(
                    "UPDATE import_run_history SET status='FAILED', "
                    "total_rows=:total, success_rows=:success, error_rows=:errors "
                    "WHERE import_run_id=:run_id"
                ),
                {
                    "total": self._total,
                    "success": self._success,
                    "errors": self._errors,
                    "run_id": self.import_run_id,
                },
            )

    def finish(self) -> ImportSummary:
        with self._conn.begin():
            self._conn.execute(
                text(
                    "UPDATE import_run_history SET status='COMPLETED', "
                    "total_rows=:total, success_rows=:success, error_rows=:errors "
                    "WHERE import_run_id=:run_id"
                ),
                {
                    "total": self._total,
                    "success": self._success,
                    "errors": self._errors,
                    "run_id": self.import_run_id,
                },
            )
        return ImportSummary(self.import_run_id, self._total, self._success, self._errors)
