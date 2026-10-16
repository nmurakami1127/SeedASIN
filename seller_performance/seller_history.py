"""セラーの取得履歴と出品成果の一覧表示（判断材料の提示のみ、自動判定・自動実行は行わない）。

指定したセラーについて、これまでのtool_execution_link（統合ツール実行履歴）を古い順に
一覧し、それぞれの実行で得られたASINのうち最終的に出品完了に至った件数を横に並べる。

「出品完了に至った件数」は、seller_attribution.first_seen_via_tool_run_idでその実行に
帰属するASINのうち、listed_at（weekly_listing_snapshotとの突き合わせ結果、
seller_performance.cli attributeが記録済みのもの）がNOT NULLである件数を数える。
そのため、このコマンドの表示内容は直近のattribute実行時点のものであり、attributeを
まだ一度も実行していない・古い場合は出品成果が実態より少なく表示され得る
（既存の運用ランブック「attribute→recommend」と同じ前提、docs/DEVIATIONS.md #29参照）。

判断材料の計算（経過日数・直近の傾向）は単純な比較のみで、複雑な統計は行わない
（ユーザー指定）。

各行の日付・件数表示:
- 表示する日付は「取得日」（tel.requested_at、統合ツールへの依頼日）。2026-09-16の変更で
  一度「取込日」（tool_output_asin.imported_at基準）に変更したが、Excel移行データでは
  imported_atが実際の依頼日ではなく「移行作業を実行した日」になってしまい、同じ
  tool_execution_linkを見るseller_recommender.py（recommendの`最終取得日`、
  tel.requested_at基準）と表示の基準が食い違う不具合になっていたため、2026-09-17に
  再びrequested_at基準へ戻した（ユーザー指摘、docs/DEVIATIONS.md #45参照）。
  requested_atはtool_execution_linkのNOT NULL列のため、この日付自体は常に存在し
  「未取込」にはならない（未取込かどうかは下記の「取込ASIN数」列で表す）。
- tool_output_asinが1件も無い場合、「未取込」（一度も取込を試みていない、取込ASIN数は
  「-」）と「0件」（取込は試みたが該当ASINが無かった、取込ASIN数は「0件」）を区別する。
  「取込を試みた」かどうかの判定は、import_run_history（import_type='TOOL_OUTPUT'）に、
  統合ツールの命名規則「{tool_run_id}_...」のprefixに一致するsource_file_nameを持つ
  完了済みレコードがあるかどうかで行う（tool_intake/pending_sync.pyと同じ判定方法、
  スキーマ変更は不要）。
- 「取込ASIN数」列（tool_output_asinの件数）はこの変更の対象外——2026-09-16の仕様のまま
  維持する。既存の「出品成果」列（listed_count）も同様に維持する（意味が異なる別の指標）。
- days_since_last_run/recent_average/recent_trend（経過日数・直近の傾向）はこの変更の
  対象外——引き続きrequested_date・listed_countのみを使う。
- 「統合ツールID」列（tool_run_id、2026-09-18追加）: セラーIDの表記ミスを訂正する際、
  delete-seller/delete-tool-run（CLAUDE.md 3節の例外、docs/DEVIATIONS.md #38参照）で
  削除する前に紐づいているtool_run_idを控える必要があるが、従来は画面上で確認する
  手段が無くDBへ直接SQLを実行するしかなかったため追加した（ユーザー指摘、
  docs/DEVIATIONS.md #46参照）。HistoryEntry.tool_run_idは元々SQLで取得済みだった
  （並び替え・突き合わせのキーとして使用）ため、表示に使うだけでSQL変更は不要だった。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, text

from seller_ledger.storefront_blocked import BlockedSellerInfo

_TREND_WINDOW = 3

_GET_HISTORY_SQL = text(
    """
    WITH toa_stats AS (
        SELECT tool_run_id, COUNT(*) AS asin_count
        FROM tool_output_asin
        WHERE marketplace = :mp
        GROUP BY tool_run_id
    ),
    attempted AS (
        SELECT tel.tool_run_id
        FROM tool_execution_link tel
        JOIN import_run_history irh
            ON irh.marketplace = tel.marketplace
           AND irh.import_type = 'TOOL_OUTPUT'
           AND irh.status = 'COMPLETED'
           AND irh.source_file_name LIKE (tel.tool_run_id || '\\_%') ESCAPE '\\'
        WHERE tel.marketplace = :mp AND tel.seller_id = :seller_id
        GROUP BY tel.tool_run_id
    ),
    listed AS (
        SELECT first_seen_via_tool_run_id AS tool_run_id, COUNT(*) AS listed_count
        FROM seller_attribution
        WHERE marketplace = :mp AND listed_at IS NOT NULL
        GROUP BY first_seen_via_tool_run_id
    )
    SELECT tel.tool_run_id,
           tel.requested_at::date AS requested_date,
           COALESCE(ts.asin_count, 0) AS asin_count,
           (COALESCE(ts.asin_count, 0) > 0 OR a.tool_run_id IS NOT NULL) AS import_attempted,
           COALESCE(l.listed_count, 0) AS listed_count
    FROM tool_execution_link tel
    LEFT JOIN toa_stats ts ON ts.tool_run_id = tel.tool_run_id
    LEFT JOIN attempted a ON a.tool_run_id = tel.tool_run_id
    LEFT JOIN listed l ON l.tool_run_id = tel.tool_run_id
    WHERE tel.marketplace = :mp AND tel.seller_id = :seller_id
    ORDER BY tel.requested_at ASC
    """
)


@dataclass
class HistoryEntry:
    tool_run_id: str
    requested_date: date
    asin_count: int
    import_attempted: bool
    listed_count: int


class SellerHistoryService:
    def get_history(self, conn: Connection, marketplace: str, seller_id: str) -> list[HistoryEntry]:
        """seller_idの取得履歴を古い順に返す。該当実行が1件も無ければ空リスト。"""
        rows = conn.execute(_GET_HISTORY_SQL, {"mp": marketplace, "seller_id": seller_id}).all()
        return [
            HistoryEntry(
                tool_run_id=r.tool_run_id,
                requested_date=r.requested_date,
                asin_count=r.asin_count,
                import_attempted=r.import_attempted,
                listed_count=r.listed_count,
            )
            for r in rows
        ]


def days_since_last_run(entries: list[HistoryEntry], today: date) -> int:
    return (today - entries[-1].requested_date).days


def recent_average(entries: list[HistoryEntry], window: int = _TREND_WINDOW) -> float:
    recent = [e.listed_count for e in entries[-window:]]
    return sum(recent) / len(recent)


def recent_trend(entries: list[HistoryEntry], window: int = _TREND_WINDOW) -> str:
    """直近window回の出品成果の傾向を、最初と最後の単純比較で判定する
    （複雑な統計は不要というユーザー指定に基づく簡易表示）。"""
    recent = [e.listed_count for e in entries[-window:]]
    if len(recent) < 2:
        return f"データ不足（{len(recent)}回のみ）"
    if recent[-1] > recent[0]:
        return "増加傾向"
    if recent[-1] < recent[0]:
        return "減少傾向"
    return "横ばい"


def format_history(
    marketplace: str,
    seller_id: str,
    entries: list[HistoryEntry],
    blocked_info: BlockedSellerInfo | None = None,
    today: date | None = None,
) -> str:
    """判断材料として画面表示する文字列を組み立てる（CLIから分離、単体テスト可能）。

    blocked_infoが与えられていれば（storefront_blocked_sellerに登録済みのセラーなら）、
    その旨と理由を先頭に表示するが、それだけで処理を終えず、続けて通常の取得履歴
    （entriesが空でも）も表示する——storefront_blocked_sellerの理由に関わらず、
    独自プログラム等でASINを取得し統合ツールの「ASINリスト」機能経由で登録することは
    可能なため、禁止セラーであることと取得履歴の有無は独立した情報として両方提示する
    （ユーザー確認済み、2026-09-10。docs/DEVIATIONS.md #36参照）。
    """
    today = today if today is not None else date.today()
    separator = "─" * 42

    lines = []
    if blocked_info is not None:
        note_part = f"、メモ: {blocked_info.note}" if blocked_info.note else ""
        lines.append(
            "※このセラーは統合ツールのストアフロントURLから取得できません"
            f"（理由: {blocked_info.reason_label}、"
            f"登録日: {blocked_info.first_noted_at.isoformat()}{note_part}）"
        )

    lines.append(f"セラー: {seller_id} (marketplace={marketplace})")
    lines.append(separator)

    if not entries:
        lines.append("取得履歴はまだありません。")
        return "\n".join(lines)

    lines.append("取得日          統合ツールID    取込ASIN数    出品成果")
    for entry in entries:
        asin_count_str = f"{entry.asin_count}件" if entry.import_attempted else "-"
        lines.append(
            f"{entry.requested_date.isoformat()}      {entry.tool_run_id}      "
            f"{asin_count_str}      {entry.listed_count}件"
        )
    lines.append(separator)

    total_listed = sum(e.listed_count for e in entries)
    lines.append(f"合計取得回数: {len(entries)}回 / 合計出品成果: {total_listed}件")
    lines.append(f"直近の取得日からの経過日数: {days_since_last_run(entries, today)}日")

    window = min(_TREND_WINDOW, len(entries))
    lines.append(
        f"直近{window}回の出品成果: 平均{recent_average(entries):.1f}件"
        f"（傾向: {recent_trend(entries)}）"
    )

    return "\n".join(lines)
