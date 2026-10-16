"""全データを分析用にCSVへ抽出する（`seller_performance.cli export-all`、ユーザー依頼、
2026-10-08、docs/DEVIATIONS.md #55参照）。

読み取り専用で、DBは一切変更しない。US/JP両方のRLSを越えて全データを見る必要がある
ため、`db.session.ReportSessionFactory`（`kpi_report_role`、BYPASSRLS）を使う
（CLAUDE.md 2節、`asin_collector.cli export-run`と同じ使用パターン）。対話式メニュー
cli.pyへの追加は不要（ユーザー指定）。

**全テーブルのダンプ**: 出力対象のテーブル一覧は、`db/table_names.py`のような
Pythonコード側の定義ではなく、`information_schema.tables`から実行時に取得する
（`table_schema='public' AND table_type='BASE TABLE'`、`alembic_version`のような
管理用テーブルは除く）。これにより「DB内の全テーブル」という依頼の字面に忠実になるほか、
`storefront_blocked_seller`（共有metadataに登録されていないテーブル、db/models.py
末尾のコメント参照）のような、Pythonコード側の一覧から漏れやすいテーブルも自動的に
含まれる。各テーブルは`SELECT *`でそのまま1テーブル=1CSVに出力する——列名はDBの列名
のまま、日時列はPythonのdatetimeオブジェクトをcsv.writerがstr()変換した値
（タイムゾーン付きで保存されている列はそのオフセットのまま、JSTへの変換はしない）。
テーブル名はinformation_schema由来（ユーザー入力を経由しない）であることを前提に
SQL文字列へ直接埋め込む——識別子はバインドパラメータ化できないため
（db/session.pyの`SET app.current_marketplace`と同じ考え方）。

**結合済みCSV（分析用）**: 生テーブルのダンプだけでは「セラーごとの状況」「統合ツール
実行1回ごとの成果」を見るために毎回手動で複数テーブルを突き合わせる必要があるため、
よく使う2つの切り口を事前に結合したCSVも出力する。

- `seller_summary.csv`: `known_seller_master`（register済みの全セラー、marketplace別）
  を起点に、`tool_execution_link`・`tool_output_asin`・`seller_attribution`・
  `storefront_blocked_seller`を集計・結合したセラー単位の1行。「取得日数(重複排除/
  全期間)」は`seller_performance.seller_recommender`の`recent_fetch_days`
  （直近90日固定）と異なり、全期間のtool_execution_link.requested_atの
  重複排除日数。「取得回数」はtool_execution_link行数そのもの（`seller_history.py`の
  「合計取得回数」と同じ定義）。「禁止セラー理由」・「禁止セラー記録日」は
  `storefront_blocked_seller`の生の値（表示用ラベルへの変換はしない——分析用途では
  生のコード値の方がフィルタ・集計しやすいため、`REASON_LABELS`は使わない）。
- `tool_run_detail.csv`: `tool_execution_link`の1行（統合ツールID単位）ごとに、
  紐づく`tool_output_asin`件数・`seller_attribution`の出品成果件数を結合した1行
  （`seller_history.py`の1セラー分の履歴表示を、全セラー分フラットに展開したもの）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, text

from db.session import ReportSessionFactory

_MANAGEMENT_TABLES = {"alembic_version"}

_LIST_TABLES_SQL = text(
    """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
    """
)

_SELLER_SUMMARY_SQL = text(
    """
    WITH tel_agg AS (
        SELECT marketplace, seller_id,
               MAX(requested_at)::date AS last_requested_date,
               COUNT(DISTINCT requested_at::date) AS fetch_days_all_time,
               COUNT(*) AS fetch_count
        FROM tool_execution_link
        GROUP BY marketplace, seller_id
    ),
    toa_by_run AS (
        SELECT marketplace, tool_run_id, COUNT(*) AS asin_count
        FROM tool_output_asin
        GROUP BY marketplace, tool_run_id
    ),
    toa_by_seller AS (
        SELECT tel.marketplace, tel.seller_id, COALESCE(SUM(t.asin_count), 0) AS imported_asin_count
        FROM tool_execution_link tel
        LEFT JOIN toa_by_run t ON t.marketplace = tel.marketplace AND t.tool_run_id = tel.tool_run_id
        GROUP BY tel.marketplace, tel.seller_id
    ),
    listed_agg AS (
        SELECT marketplace, seller_id, COUNT(*) FILTER (WHERE listed_at IS NOT NULL) AS total_listed
        FROM seller_attribution
        GROUP BY marketplace, seller_id
    )
    SELECT
        ksm.marketplace,
        ksm.seller_id,
        ksm.first_known_at,
        tel_agg.last_requested_date,
        COALESCE(tel_agg.fetch_days_all_time, 0) AS fetch_days_all_time,
        COALESCE(tel_agg.fetch_count, 0) AS fetch_count,
        COALESCE(toa_by_seller.imported_asin_count, 0) AS imported_asin_count,
        COALESCE(listed_agg.total_listed, 0) AS total_listed,
        sbs.reason AS blocked_reason,
        sbs.first_noted_at AS blocked_first_noted_at
    FROM known_seller_master ksm
    LEFT JOIN tel_agg ON tel_agg.marketplace = ksm.marketplace AND tel_agg.seller_id = ksm.seller_id
    LEFT JOIN toa_by_seller ON toa_by_seller.marketplace = ksm.marketplace AND toa_by_seller.seller_id = ksm.seller_id
    LEFT JOIN listed_agg ON listed_agg.marketplace = ksm.marketplace AND listed_agg.seller_id = ksm.seller_id
    LEFT JOIN storefront_blocked_seller sbs ON sbs.seller_id = ksm.seller_id
    ORDER BY ksm.marketplace, ksm.seller_id
    """
)

_SELLER_SUMMARY_HEADER = [
    "marketplace",
    "seller_id",
    "初回登録日",
    "最終取得日",
    "取得日数(重複排除/全期間)",
    "取得回数",
    "取込ASIN数",
    "合計出品成果",
    "禁止セラー理由",
    "禁止セラー記録日",
]

_TOOL_RUN_DETAIL_SQL = text(
    """
    WITH toa_by_run AS (
        SELECT marketplace, tool_run_id, COUNT(*) AS asin_count
        FROM tool_output_asin
        GROUP BY marketplace, tool_run_id
    ),
    listed_by_run AS (
        SELECT marketplace, first_seen_via_tool_run_id AS tool_run_id,
               COUNT(*) FILTER (WHERE listed_at IS NOT NULL) AS listed_count
        FROM seller_attribution
        GROUP BY marketplace, first_seen_via_tool_run_id
    )
    SELECT
        tel.marketplace,
        tel.seller_id,
        tel.tool_run_id,
        tel.requested_at::date AS requested_date,
        COALESCE(t.asin_count, 0) AS imported_asin_count,
        COALESCE(l.listed_count, 0) AS listed_count
    FROM tool_execution_link tel
    LEFT JOIN toa_by_run t ON t.marketplace = tel.marketplace AND t.tool_run_id = tel.tool_run_id
    LEFT JOIN listed_by_run l ON l.marketplace = tel.marketplace AND l.tool_run_id = tel.tool_run_id
    ORDER BY tel.marketplace, tel.seller_id, tel.requested_at
    """
)

_TOOL_RUN_DETAIL_HEADER = [
    "marketplace",
    "seller_id",
    "統合ツールID",
    "取得日",
    "取込ASIN数",
    "出品成果",
]


@dataclass
class ExportAllResult:
    output_dir: Path
    file_row_counts: dict[str, int] = field(default_factory=dict)  # ファイル名 -> 行数（挿入順）


def list_exportable_tables(conn: Connection) -> list[str]:
    """`information_schema`から、管理用テーブル（alembic_version）を除く全テーブル名を
    テーブル名の昇順で返す。Pythonコード側の静的なテーブル一覧（db/table_names.py）に
    依存しないため、将来テーブルが増減してもこの関数は変更不要（モジュールdocstring参照）。
    """
    names = conn.execute(_LIST_TABLES_SQL).scalars().all()
    return [name for name in names if name not in _MANAGEMENT_TABLES]


def _write_rows_to_csv(header: list[str], rows, output_path: Path) -> int:
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        row_count = 0
        for row in rows:
            writer.writerow(row)
            row_count += 1
    return row_count


class DataExportService:
    def export_table_csv(self, conn: Connection, table_name: str, output_path: Path) -> int:
        """table_nameは必ずlist_exportable_tables()の戻り値を渡すこと（information_schema
        由来の信頼できる出処で、ユーザー入力を経由しない前提。モジュールdocstring参照）。"""
        result = conn.execute(text(f'SELECT * FROM "{table_name}"'))
        header = list(result.keys())
        return _write_rows_to_csv(header, result, output_path)

    def export_seller_summary_csv(self, conn: Connection, output_path: Path) -> int:
        result = conn.execute(_SELLER_SUMMARY_SQL)
        return _write_rows_to_csv(_SELLER_SUMMARY_HEADER, result, output_path)

    def export_tool_run_detail_csv(self, conn: Connection, output_path: Path) -> int:
        result = conn.execute(_TOOL_RUN_DETAIL_SQL)
        return _write_rows_to_csv(_TOOL_RUN_DETAIL_HEADER, result, output_path)

    def export_all(self, factory: ReportSessionFactory, output_dir: Path) -> ExportAllResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_row_counts: dict[str, int] = {}

        with factory.get_session() as conn:
            for table_name in list_exportable_tables(conn):
                path = output_dir / f"{table_name}.csv"
                file_row_counts[path.name] = self.export_table_csv(conn, table_name, path)

            path = output_dir / "seller_summary.csv"
            file_row_counts[path.name] = self.export_seller_summary_csv(conn, path)

            path = output_dir / "tool_run_detail.csv"
            file_row_counts[path.name] = self.export_tool_run_detail_csv(conn, path)

        return ExportAllResult(output_dir=output_dir, file_row_counts=file_row_counts)
