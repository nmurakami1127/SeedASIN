"""統合ツール出力ファイル（26列）の取込。ASIN・取得日時のみtool_output_asinへ登録する
（docs/phase3_implementation_guide.md 2.4節: 価格・カテゴリー等は取り込まない、
ユーザー確認済み2026-09-03）。

取込完了後、tool_execution_link経由でこのtool_run_idに紐づくセラーを特定し、
known_seller_master.latest_product_count/latest_checked_atを更新する
（docs/DEVIATIONS.md #15: この更新はseller_ledger/seller_performanceではなく
tool_intakeの責務、ユーザー確認済み2026-09-03）。tool_execution_linkが未記録
（seller_ledger.tool_run_link.record()が呼ばれていない）場合は更新対象が無いため
何も起きない（エラーにはしない）。

実ファイルのヘッダー列名（2026-09-03にユーザーから提供）:
ASIN, バリエーション, 商品名, 出品者数, ブランド, 最安値, 重量（kg）,
体積重量（縦×横×高さ/5000）, 大カテゴリー名, 大カテゴリー順位, 中カテゴリー名,
中カテゴリー順位, 最安値セラーの出品国, 品番, 型番, 日本出品者数, 日本最安値,
日本大カテゴリー名, 日本大カテゴリー順位, 日本中カテゴリー名, 日本中カテゴリー順位,
日米差益, (空), (空), 取得日時, (空)
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from db.session import SessionFactory
from tool_intake.csv_utils import column_index, parse_datetime_flexible, read_csv_rows
from tool_intake.import_run import ImportRunRecorder, ImportSummary

_IMPORT_TYPE = "TOOL_OUTPUT"
_ASIN_COLUMN = "ASIN"
_ACQUIRED_AT_COLUMN = "取得日時"


class IntegrationToolImportService:
    def import_file(
        self, session_factory: SessionFactory, marketplace: str, tool_run_id: str, path: Path
    ) -> ImportSummary:
        """tool_run_idは呼び出し元がファイル名から抽出して渡す（例: '7128337_....csv'の
        '7128337'部分）。既にtool_output_asinに存在する(marketplace, tool_run_id, asin)は
        ON CONFLICT DO NOTHINGで無視するため、同一ファイルの再取込は冪等。
        """
        header, data_rows = read_csv_rows(path)
        asin_idx = column_index(header, _ASIN_COLUMN)
        acquired_idx = column_index(header, _ACQUIRED_AT_COLUMN)

        with session_factory.get_session(marketplace) as conn:
            recorder = ImportRunRecorder(conn, marketplace, _IMPORT_TYPE, path.name)

            for row_number, row in enumerate(data_rows, start=1):
                try:
                    asin = row[asin_idx].strip()
                    if not asin:
                        raise ValueError("ASIN列が空です")
                    acquired_at = parse_datetime_flexible(row[acquired_idx])
                except (ValueError, IndexError) as exc:
                    recorder.record_error(row_number, ",".join(row), "IM-TOOL-01", str(exc))
                    continue

                with conn.begin():
                    conn.execute(
                        text(
                            "INSERT INTO tool_output_asin "
                            "(marketplace, tool_run_id, asin, acquired_at) "
                            "VALUES (:mp, :run_id, :asin, :acquired_at) "
                            "ON CONFLICT (marketplace, tool_run_id, asin) DO NOTHING"
                        ),
                        {
                            "mp": marketplace,
                            "run_id": tool_run_id,
                            "asin": asin,
                            "acquired_at": acquired_at,
                        },
                    )
                recorder.record_success()

            with conn.begin():
                conn.execute(
                    text(
                        """
                        UPDATE known_seller_master ksm
                        SET latest_product_count = sub.cnt, latest_checked_at = now()
                        FROM (
                            SELECT tel.marketplace, tel.seller_id, count(*) AS cnt
                            FROM tool_execution_link tel
                            JOIN tool_output_asin toa
                                ON toa.marketplace = tel.marketplace AND toa.tool_run_id = tel.tool_run_id
                            WHERE tel.marketplace = :mp AND tel.tool_run_id = :run_id
                            GROUP BY tel.marketplace, tel.seller_id
                        ) sub
                        WHERE ksm.marketplace = sub.marketplace AND ksm.seller_id = sub.seller_id
                        """
                    ),
                    {"mp": marketplace, "run_id": tool_run_id},
                )

            return recorder.finish()
