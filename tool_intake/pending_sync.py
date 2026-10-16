"""tool_execution_linkにはあるがtool_output_asinがまだ1件も無いtool_run_idを検出し、
processed_file_lookup.find_tool_output_files()で自動検出できるものは取込を実行する
（`tool_intake.cli sync-pending` / 対話式メニューcli.py「10」の共通実装、ユーザー依頼）。

「未取込」（一度も取込を試みていない）と「0件」（取込を試みたがファイル内に有効な
ASIN行が無かった）を区別し、対象は前者のみとする（ユーザー確認済み、2026-09-16）。
「0件」だったtool_run_idを毎回再チェックしても、同じファイルが見つかる限り結果は
変わらないため対象から除外する。判定は`import_run_history`に、
`import_type='TOOL_OUTPUT'`かつ`source_file_name`が統合ツールの命名規則
「{tool_run_id}_...」のprefixに一致する完了済みレコードがあるかどうかで行う
（この命名規則はprocessed_file_lookup.find_tool_output_filesおよび
tool_intake/log_ingest.pyのlog-csv列の実データ双方で確認済みのため、スキーマ変更
（tool_run_id列の追加等）は不要と判断した）。

見つかったファイルが複数件（ambiguous）の場合も、非対話のバッチ処理のため自動選択せず
「未検出」側に残す（`tool_intake.cli import-tool-output`の--file省略時と同じ方針）。
1件の取込に失敗しても他のtool_run_idの処理は継続する（CLAUDE.md 6節のエラー分離）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, text

from db.session import SessionFactory
from tool_intake.integration_tool_import import IntegrationToolImportService
from tool_intake.processed_file_lookup import find_tool_output_files

_FIND_PENDING_SQL = text(
    """
    WITH attempted AS (
        SELECT tel.tool_run_id
        FROM tool_execution_link tel
        JOIN import_run_history irh
            ON irh.marketplace = tel.marketplace
           AND irh.import_type = 'TOOL_OUTPUT'
           AND irh.status = 'COMPLETED'
           AND irh.source_file_name LIKE (tel.tool_run_id || '\\_%') ESCAPE '\\'
        WHERE tel.marketplace = :mp
        GROUP BY tel.tool_run_id
    )
    SELECT tel.seller_id, tel.tool_run_id, tel.requested_at
    FROM tool_execution_link tel
    WHERE tel.marketplace = :mp
      AND NOT EXISTS (
          SELECT 1 FROM tool_output_asin toa
          WHERE toa.marketplace = tel.marketplace AND toa.tool_run_id = tel.tool_run_id
      )
      AND tel.tool_run_id NOT IN (SELECT tool_run_id FROM attempted)
    ORDER BY tel.requested_at ASC
    """
)


@dataclass
class PendingRun:
    seller_id: str
    tool_run_id: str
    requested_at: datetime


@dataclass
class PendingSyncResult:
    imported: list[PendingRun] = field(default_factory=list)
    not_found: list[PendingRun] = field(default_factory=list)
    errored: list[tuple[PendingRun, str]] = field(default_factory=list)

    def format(self) -> str:
        total = len(self.imported) + len(self.not_found) + len(self.errored)
        lines = [
            f"未取込チェック対象: {total}件",
            f"自動取込できた: {len(self.imported)}件",
        ]
        for run in self.imported:
            lines.append(f"  - tool_run_id={run.tool_run_id} seller_id={run.seller_id}")
        lines.append(f"取込エラー: {len(self.errored)}件")
        for run, message in self.errored:
            lines.append(f"  - tool_run_id={run.tool_run_id} seller_id={run.seller_id}: {message}")
        lines.append(f"まだ未検出: {len(self.not_found)}件")
        for run in self.not_found:
            lines.append(
                f"  - tool_run_id={run.tool_run_id} seller_id={run.seller_id} "
                f"依頼日時={run.requested_at.isoformat()}"
            )
        return "\n".join(lines)


class PendingSyncService:
    def find_pending(self, conn: Connection, marketplace: str) -> list[PendingRun]:
        rows = conn.execute(_FIND_PENDING_SQL, {"mp": marketplace}).all()
        return [
            PendingRun(seller_id=r.seller_id, tool_run_id=r.tool_run_id, requested_at=r.requested_at)
            for r in rows
        ]

    def sync(self, session_factory: SessionFactory, marketplace: str) -> PendingSyncResult:
        with session_factory.get_session(marketplace) as conn:
            pending = self.find_pending(conn, marketplace)

        result = PendingSyncResult()
        for run in pending:
            candidates = find_tool_output_files(marketplace, run.tool_run_id)
            if len(candidates) != 1:
                result.not_found.append(run)
                continue
            try:
                IntegrationToolImportService().import_file(
                    session_factory, marketplace, run.tool_run_id, candidates[0]
                )
            except Exception as exc:  # 1件のエラーで残りのtool_run_idの処理を止めない（CLAUDE.md 6節）
                result.errored.append((run, str(exc)))
                continue
            result.imported.append(run)

        return result
