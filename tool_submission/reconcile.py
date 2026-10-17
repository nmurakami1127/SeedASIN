"""統合ツールのログ画面（保存HTMLまたは画面から読んだ行）と投入台帳の照合。

実行IDは、ログ画面の`<tr data-seq>`（既存の`tool_intake/log_ingest.py`と同じ）。投入後に、メモ・種別(asin_list)・国設定が
投入計画と一致する**新しい行**を探して実行IDを確定する。1件に決まらなければ確定しない（0件=見つからない、複数=二重投入の疑い）。
ログ画面にはASINの内容は出ないため、内容の一致は確認できない（メモと「投入前に無かった行」だけが根拠）。
"""
from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser


@dataclass(frozen=True)
class LogEntry:
    seq: int
    data_type: str  # 'asin_list' / 'store_url' / 'keyword' 等
    country: str  # 'jp' / 'us'（ツールがASINを調べる国。業務方向とは別）
    memo: str
    status: str
    csv: str | None


class LogUnreadable(Exception):
    """ログ画面を読めない（未ログイン・構造変化・通信失敗）。結果が分からない間は再投入しない。"""


def parse_log_entries(html: str) -> list[LogEntry]:
    tree = HTMLParser(html)
    out: list[LogEntry] = []
    for tr in tree.css("tr[data-seq]"):
        seq = (tr.attributes.get("data-seq") or "").strip()
        if not seq.isdigit():
            continue
        memo_node = tr.css_first("textarea.memo")
        status_node = tr.css_first("td.log-status")
        csv_node = tr.css_first("td.log-csv")
        csv_text = csv_node.text().strip() if csv_node else "-"
        out.append(LogEntry(
            int(seq), (tr.attributes.get("data-type") or "").strip().lower(), (tr.attributes.get("data-country") or "").strip().lower(),
            memo_node.text().strip() if memo_node else "", status_node.text().strip() if status_node else "",
            None if csv_text in ("", "-") else csv_text,
        ))
    return out


@dataclass(frozen=True)
class MatchDecision:
    kind: str  # CONFIRMED / AMBIGUOUS / NOT_FOUND
    candidates: tuple[int, ...] = ()

    @property
    def tool_run_id(self) -> str | None:
        return str(self.candidates[0]) if self.kind == "CONFIRMED" else None


def match_new_entries(entries: list[LogEntry], memo: str, tool_country: str | None, *, before_run_ids: set[int] | None = None,
                      exclude_run_ids: frozenset[int] | set[int] = frozenset()) -> MatchDecision:
    """メモ・種別・国設定が一致する行のうち、投入前に無かった（before_run_idsに無い）行を探す。
    before_run_ids=None は「基準なし」（手動投入の突き合わせ）: 他の投入で確定済みの実行ID(exclude_run_ids)だけを除く。"""
    cands = sorted(
        e.seq for e in entries
        if e.data_type == "asin_list" and e.memo.strip() == memo and (tool_country is None or e.country == tool_country.lower())
        and e.seq not in exclude_run_ids and (before_run_ids is None or e.seq not in before_run_ids)
    )
    if not cands:
        return MatchDecision("NOT_FOUND")
    return MatchDecision("CONFIRMED" if len(cands) == 1 else "AMBIGUOUS", tuple(cands))
