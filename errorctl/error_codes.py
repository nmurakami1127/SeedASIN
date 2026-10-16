"""エラーコード定義（実装指示書7章の全件）。

各コードを実際にraiseする例外クラスは、それを送出するモジュール（M1/M3/M4/M7/M8等）の
実装フェーズ（Phase 3以降）でerrorctl/exceptions.pyに追加する。ここでは値の定義のみ行う
（未実装モジュールの例外クラスを先回りして作らない。CLAUDE.mdの過剰設計禁止方針）。
"""
from enum import StrEnum


class ErrorCode(StrEnum):
    SR_01_01 = "SR-01-01"  # M1: 探索先ページ取得タイムアウト（○リトライ対象、RETRY_PENDING）
    SR_01_02 = "SR-01-02"  # M1: 探索先ページ構造解析失敗（Amazon側UI変更等）（○リトライ対象）
    EL_02_01 = "EL-02-01"  # M4: 商品詳細ページ取得失敗（○リトライ対象）
    EL_02_02 = "EL-02-02"  # M4: 出品可否表示の解析不能（○リトライ対象）
    EL_04_01 = "EL-04-01"  # M8: 登録前チェックで有効期限切れ検出（登録ブロック、×リトライ対象外）
    IM_03_01 = "IM-03-01"  # M3/B1: 既知ASIN取込CSV行の必須項目欠落（×、当該行スキップ）
    IM_07_01 = "IM-07-01"  # M7/B2: 既知Seller取込CSV行の必須項目欠落（×、当該行スキップ）
    IM_TOOL_01 = "IM-TOOL-01"  # tool_intake: 統合ツール出力CSV行の必須項目欠落・解析失敗（×、当該行スキップ）
    IM_REPORT_01 = "IM-REPORT-01"  # origin_selection: ビジネスレポートCSV行の必須項目欠落・解析失敗（×、当該行を除外して取込は継続）
    IM_WEEKLY_01 = "IM-WEEKLY-01"  # tool_intake: 週次出品完了リストCSV行の必須項目欠落・解析失敗（×、当該行スキップ）
    JD_08_01 = "JD-08-01"  # M8: Seller明細のseller_id重複登録（同一seller_check_id内）（×）
    MP_11_01 = "MP-11-01"  # M11: RLS違反（app.current_marketplace未設定でのアクセス）（×、システムエラーとして即時停止）
    RT_06_01 = "RT-06-01"  # M12/B6: リトライ上限到達（RETRY_EXHAUSTEDへ遷移）（×、手動再処理指示待ち）
