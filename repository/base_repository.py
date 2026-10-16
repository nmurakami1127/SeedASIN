"""M11 データ保存基盤: UPSERT対象テーブルと追記専用テーブルでメソッドを明確に分離する。

追記専用テーブル（asin_discovery_event, eligibility_history, seed_output_history,
processing_error_log, import_error_log, kpi_snapshot）に対しては
append_only_insert() / append_only_insert_many() のみを使用し、
upsert()を呼び出す実装を絶対に混入させないこと（CLAUDE.md 3節）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Connection, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert


class BaseRepository:
    def __init__(self, table: Table) -> None:
        self.table = table

    def upsert(
        self,
        conn: Connection,
        values: Mapping[str, Any],
        *,
        index_elements: Sequence[str],
        update_columns: Sequence[str] | None = None,
    ) -> None:
        """INSERT ... ON CONFLICT (index_elements) DO UPDATE。

        update_columnsを省略した場合はindex_elements以外の全列を更新対象にする（ただし主キー列は
        常に除外する。seller_check等、ON CONFLICTの対象がUNIQUE制約でありBIGSERIAL主キーとは別の
        テーブルで、主キー列をSET句に含めてしまう事故を防ぐため）。
        asin_master.first_originはON CONFLICT DO UPDATE句から常に除外すること
        （CLAUDE.md 5節）。誤って含めた場合はここで例外を送出する。
        """
        stmt = pg_insert(self.table).values(**values)
        pk_columns = {c.name for c in self.table.primary_key.columns}
        set_columns = list(update_columns) if update_columns is not None else [
            c.name for c in self.table.columns if c.name not in index_elements and c.name not in pk_columns
        ]

        if self.table.name == "asin_master" and "first_origin" in set_columns:
            raise ValueError(
                "first_origin must never appear in asin_master's ON CONFLICT DO UPDATE "
                "SET clause (CLAUDE.md rule 5)"
            )

        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={col: stmt.excluded[col] for col in set_columns},
        )
        conn.execute(stmt)

    def append_only_insert(self, conn: Connection, values: Mapping[str, Any]) -> None:
        conn.execute(self.table.insert().values(**values))

    def append_only_insert_many(self, conn: Connection, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        conn.execute(self.table.insert(), rows)
