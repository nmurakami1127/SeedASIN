"""週次出品完了リスト（listing-XXXXXファイル）の取込。米国セラー分21列・日本セラー分23列
（関税・州税列の有無）を、marketplaceパラメータから決め打ちし、実際のヘッダー列数と
食い違えばエラーで中断する（ファイル内容からの自動判別はしない。ユーザー確認済み2026-09-03）。

SKU列（例: 'USNEW1751638'）が空の行は「登録日時ベースで抽出した、出品中のASIN」に該当しない
（docs/SeedASIN_スコープ整理_20260902.md 9節）ため取込対象外とし、スキップする（エラー扱いに
しない。weekly_listing_snapshot.skuはNOT NULLのため、この扱いと整合する）。

実ファイルのヘッダー列名（2026-09-03にユーザーから提供）:
米国セラー分（21列）: ASIN, 画像, 商品名, 利益, 売値/最安値, FBA出荷, 送料, 諸経費,
  幅インチ、長さインチ、高さインチ, 在庫数, 登録日時, SKU, メーカー, 日本最安値, 出品価格,
  更新日時, 販売手数料, 米国最安値, ツール外出品, 日本Amazon, 米国Amazon
日本セラー分（23列）: 米国分と同じ列に加え、列14が「販売価格」（米国分は「出品価格」）、
  かつ列18/19に「関税」「州税」が追加される。ASIN・SKU・登録日時の列名・位置は共通の
  ため、この名称差異は本取込処理には影響しない（実ファイルで確認済み、
  docs/DEVIATIONS.md #33参照）。

snapshot_dateは、CSV内の「登録日時」列の値（日付部分のみ）を行ごとに使う
（2026-09-09、ユーザー確認済み・仕様変更。docs/DEVIATIONS.md #33参照）。
以前は取込バッチを実行した日付を全行共通で使っていたが、バッチ実行日は「実際に
そのASINが登録された日」の不正確な代用品でしかなく、ファイル内に正確な日時が
既に存在するため、そちらを正として使う方針に変更した。「登録日時」列の値が
不正な形式・欠落している行は、ASIN列が空の行と同様にimport_error_logへ記録して
スキップし、処理を継続する（1行のエラーがバッチ全体を止めないCLAUDE.md 6節の方針）。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from db.session import SessionFactory
from tool_intake.csv_utils import column_index, parse_datetime_flexible, read_csv_rows
from tool_intake.import_run import ImportRunRecorder, ImportSummary

_IMPORT_TYPE = "WEEKLY_LISTING"
_ASIN_COLUMN = "ASIN"
_SKU_COLUMN = "SKU"
_REGISTERED_AT_COLUMN = "登録日時"
_EXPECTED_COLUMN_COUNT = {"US": 21, "JP": 23}


class WeeklyListingImportService:
    def import_file(self, session_factory: SessionFactory, marketplace: str, path: Path) -> ImportSummary:
        header, data_rows = read_csv_rows(path)
        expected = _EXPECTED_COLUMN_COUNT[marketplace]
        if len(header) != expected:
            raise ValueError(
                f"marketplace={marketplace!r}は{expected}列を想定しているが、"
                f"実際のヘッダーは{len(header)}列だった: {header}"
            )
        asin_idx = column_index(header, _ASIN_COLUMN)
        sku_idx = column_index(header, _SKU_COLUMN)
        registered_at_idx = column_index(header, _REGISTERED_AT_COLUMN)

        with session_factory.get_session(marketplace) as conn:
            recorder = ImportRunRecorder(conn, marketplace, _IMPORT_TYPE, path.name)

            for row_number, row in enumerate(data_rows, start=1):
                try:
                    asin = row[asin_idx].strip()
                    sku = row[sku_idx].strip()
                    if not asin:
                        raise ValueError("ASIN列が空です")
                except IndexError as exc:
                    recorder.record_error(row_number, ",".join(row), "IM-WEEKLY-01", str(exc))
                    continue

                if not sku:
                    # 出品中でない（登録日時ベースの抽出対象外）行はエラーではなくスキップ。
                    recorder.record_skip()
                    continue

                try:
                    registered_at = parse_datetime_flexible(row[registered_at_idx])
                except (ValueError, IndexError) as exc:
                    recorder.record_error(row_number, ",".join(row), "IM-WEEKLY-01", str(exc))
                    continue

                with conn.begin():
                    conn.execute(
                        text(
                            "INSERT INTO weekly_listing_snapshot "
                            "(marketplace, asin, sku, snapshot_date) "
                            "VALUES (:mp, :asin, :sku, :snapshot_date) "
                            "ON CONFLICT (marketplace, asin, snapshot_date) DO NOTHING"
                        ),
                        {
                            "mp": marketplace,
                            "asin": asin,
                            "sku": sku,
                            "snapshot_date": registered_at.date(),
                        },
                    )
                recorder.record_success()

            return recorder.finish()
