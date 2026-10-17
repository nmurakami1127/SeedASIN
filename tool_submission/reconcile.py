"""統合ツールのログ画面（保存HTMLまたは画面から読んだ行）の解析と、送信直後の「新しい行」の特定。

実行IDは、ログ画面の`<tr data-seq>`（既存の`tool_intake/log_ingest.py`と同じ）。**メモはログ画面で投入の後に付ける**ので、
「メモ一致で新しい実行IDを探す」ことはできない。特定の根拠は次の組み合わせだけ:
1. 送信前に控えたログID一覧（基準）に無い行（既存の取込が後から取り込んだ行を過去として除外しない。除外は送信前のスナップショット基準）
2. 種別が`asin_list`（このASINリスト入力で作られる種別。保存HTMLで確認済み）
3. メモが空（または、再開時に付けようとしている文字列と同じ）。別のメモが既にある新規行は他の人の投入とみなして候補にしない
4. 他の投入の実行IDとして特定済みでない
候補がちょうど1件のときだけ特定する。複数なら対応を一意に確認できないので、適当な行にメモを付けず停止する（AMBIGUOUS）。
`data-country`はASINリスト入力では国指定がないため判定に使わない（観測できれば保存するだけ）。ログ画面にはASINの内容は出ないため、
入力内容の一致はここでは確認できない（取得完了後に出力CSVで確認する`verify_output`）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from selectolax.parser import HTMLParser


@dataclass(frozen=True)
class LogEntry:
    seq: int
    data_type: str  # 'asin_list' / 'store_url' / 'keyword' 等
    country: str  # 'jp' / 'us'（観測値。ASINリスト入力では判定に使わない）
    memo: str
    status: str
    csv: str | None


def entries_from_json(rows: list[dict]) -> list[LogEntry]:
    """実画面から読み取った行（seq/data_type/country/memo/status/csv）のスナップショット。"""
    return [LogEntry(int(r["seq"]), str(r.get("data_type", "")).lower(), str(r.get("country", "")).lower(), str(r.get("memo", "")).strip(),
                     str(r.get("status", "")), r.get("csv") or None) for r in rows]


def load_log_entries(path) -> list[LogEntry]:
    """.jsonならスナップショット、それ以外は保存HTMLを読む。"""
    import json
    from pathlib import Path as _P

    from tool_intake.csv_utils import open_text_auto

    p = _P(path)
    if p.suffix.lower() == ".json":
        return entries_from_json(json.loads(p.read_text(encoding="utf-8")))
    return parse_log_entries(open_text_auto(p))


def single_row_html(html: str, run_id: int, expected_memo: str) -> str:
    """ログ画面のHTMLから、対象の実行IDの行**だけ**を取り出した最小のHTMLを作る（既存のingest-logが無関係な実行や保留状態まで
    更新しないため）。行が1件に決まらない・メモが期待と違うなら ValueError。"""
    tree = HTMLParser(html)
    rows = [tr for tr in tree.css("tr[data-seq]") if (tr.attributes.get("data-seq") or "").strip() == str(run_id)]
    if len(rows) != 1:
        raise ValueError(f"実行ID{run_id}の行が{len(rows)}件あります（1件に決まりません）")
    memo_node = rows[0].css_first("textarea.memo")
    memo = memo_node.text().strip() if memo_node else ""
    if memo != expected_memo:
        raise ValueError(f"実行ID{run_id}のメモが{memo!r}で、期待の{expected_memo!r}と違います（メモ保存の確認前は取り込みません）")
    return "<table>" + rows[0].html + "</table>"


class LogUnreadable(Exception):
    """ログ画面を読めない（未ログイン・構造変化・通信失敗）。結果が分からない間は再送しない。"""


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
class IdentifyDecision:
    kind: str  # IDENTIFIED / AMBIGUOUS / NOT_FOUND
    candidates: tuple[int, ...] = ()
    country: str | None = None

    @property
    def tool_run_id(self) -> str | None:
        return str(self.candidates[0]) if self.kind == "IDENTIFIED" else None


def identify_new_entry(entries: list[LogEntry], before_run_ids: set[int], *, expected_memo: str | None = None,
                       exclude_run_ids: set[int] | frozenset[int] = frozenset(), response_run_id: int | None = None) -> IdentifyDecision:
    """送信前の基準に無い新しいasin_list行を探す。response_run_id（送信直後の画面応答から得た実行ID）があれば、それが新しい行として
    ログに現れていることを確認して採用する（他の新しい行があっても、応答が示す行だけを採用する）。"""
    new = [e for e in entries if e.seq not in before_run_ids and e.seq not in exclude_run_ids and e.data_type == "asin_list"
           and e.memo.strip() in ("", expected_memo or "")]
    if response_run_id is not None:
        hit = [e for e in new if e.seq == response_run_id]
        return IdentifyDecision("IDENTIFIED", (response_run_id,), hit[0].country or None) if hit else IdentifyDecision("NOT_FOUND")
    if not new:
        return IdentifyDecision("NOT_FOUND")
    cands = tuple(sorted(e.seq for e in new))
    if len(cands) == 1:
        return IdentifyDecision("IDENTIFIED", cands, new[0].country or None)
    return IdentifyDecision("AMBIGUOUS", cands)


def verify_output(input_asins: list[str], output_csv: Path) -> dict:
    """統合ツールの出力CSV（取得完了後）のASIN列と、送信した入力を比べる（送信結果と実行IDの対応の事後確認）。"""
    from tool_intake.csv_utils import open_text_auto

    text_ = open_text_auto(Path(output_csv))
    rows = list(csv.reader(text_.splitlines()))
    if not rows:
        return {"ok": False, "reason": "出力CSVが空です"}
    header = [h.strip().lower() for h in rows[0]]
    if "asin" not in header:
        return {"ok": False, "reason": "ASIN列がありません"}
    idx = header.index("asin")
    got = {r[idx].strip().upper() for r in rows[1:] if len(r) > idx and r[idx].strip()}
    want = {a.strip().upper() for a in input_asins}
    return {"ok": bool(got) and got <= want, "input": len(want), "output": len(got), "outside_input": len(got - want),
            "missing_from_output": len(want - got)}
