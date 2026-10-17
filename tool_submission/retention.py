"""統合ツールのログ300件上限への対策: 新しい送信の前に、削除される可能性のある行の出力が保全されているかを確認する。

統合ツールのログ画面は**300行**で、新しい送信のたびに**最古の行と出力CSV**がサービス側で削除される（実画面で確認: run 82の送信で
7170081、run 83で7170085が消えた。応答の`csv_del.php`）。この削除は送信の副作用で取り消せないため、送信の**前**に、消える行を特定し、
出力が失われても困らない状態か確認する。

行ごとの規則（削除される行 = ログのIDが小さい順に `件数 + 追加数 - 300` 行）:
- `取得完了`: **元の出力CSVがローカルに保全されていること**が条件（既存のautomation.pyが保存・処理した`C:\\work\\input\\processed\\…`や
  `incoming`の「<ID>_…csv」）。**DBへのASIN取込（tool_output_asin）は、ASINと取得日時しか入れないため、元CSVの全列を保全したことには
  ならない**ので、保全とは区別して報告する（保全が無ければ送信を止める）。DB取込が必要なのに未取込なら「取込が必要な対象」として報告する
  （無関係な取込・大量ダウンロードは勝手にしない）。
- `待機中` / `取得中`: 削除されると出力を失う可能性があるので送信を止める。
- `禁止セラー`: 出力が無いので保全の対象外（記録だけ残す）。
- 上記以外・詳細が不明: 判定不能として送信を止める。
削除される行のID・メモ・状態・CSV名・ローカルの保全先(sha256)・DB取込の有無は、ローカルの記録（JSONL）に残す（行が消えた後も追えるように）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tool_submission.reconcile import LogEntry

LOG_LIMIT = 300  # 実画面で観測した保持行数（公式の仕様としては未確認）
LOCAL_ORIGINAL_DIRS = [
    Path(r"C:\work\input\processed\primary_check"), Path(r"C:\work\input\processed\ex_primary_check"),
    Path(r"C:\work\input\incoming\primary_check"), Path(r"C:\work\input\incoming\ex_primary_check"),
]
_PREFIX_MARKET = {"輸出": "US", "輸入": "JP"}
_COUNTRY_MARKET = {"jp": "JP", "us": "US"}


@dataclass
class RetentionItem:
    seq: int
    status: str
    memo: str
    csv: str | None
    time: str
    market: str | None
    local_files: list[str] = field(default_factory=list)
    local_sha256: str | None = None
    db_rows: int | None = None
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def rows_at_risk(seqs: list[int], will_add: int = 1, limit: int = LOG_LIMIT) -> list[int]:
    """今回の送信で削除される可能性のある行（IDが小さい順）。ログが上限に達していなければ無し。"""
    n = max(0, len(set(seqs)) + will_add - limit)
    return sorted(set(seqs))[:n]


def market_of(e: LogEntry) -> str | None:
    """既存のlog_ingestと同じ規則（メモの輸出/輸入の接頭辞 → 市場。無ければdata-country）。"""
    for prefix, m in _PREFIX_MARKET.items():
        if e.memo.startswith(prefix):
            return m
    return _COUNTRY_MARKET.get(e.country)


def _safe_name(name: str) -> str:  # automation.pyのsafe_nameと同じ（Windowsで使えない文字を_に）
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")


def find_local_originals(e: LogEntry, dirs: list[Path]) -> list[Path]:
    """元の出力CSV（log-csvの名前、または「<ID>_」で始まるファイル）がローカルにあるか。"""
    names = {_safe_name(e.csv)} if e.csv else set()
    found: list[Path] = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        for p in sorted(Path(d).iterdir()):
            if p.is_file() and p.suffix.lower() == ".csv" and (p.name in names or p.name.startswith(f"{e.seq}_")):
                found.append(p)
    return found


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_retention(entries: list[LogEntry], *, will_add: int = 1, dirs: list[Path] | None = None,
                    db_rows: Callable[[str, int], int | None] | None = None, limit: int = LOG_LIMIT) -> list[RetentionItem]:
    """削除される可能性のある行ごとの保全状況。blockersがあれば新規送信を止める。"""
    dirs = LOCAL_ORIGINAL_DIRS if dirs is None else dirs
    by_seq = {e.seq: e for e in entries}
    out: list[RetentionItem] = []
    for seq in rows_at_risk(list(by_seq), will_add, limit):
        e = by_seq[seq]
        it = RetentionItem(seq, e.status, e.memo, e.csv, getattr(e, "time", ""), market_of(e))
        if e.status == "取得完了":
            files = find_local_originals(e, dirs)
            it.local_files = [str(p) for p in files]
            if files:
                it.local_sha256 = _sha256(files[0])
            else:
                it.blockers.append("元の出力CSVがローカルに保全されていません（この行が削除されると失われます）")
            if db_rows is not None and it.market:
                try:
                    it.db_rows = db_rows(it.market, seq)
                except Exception as exc:  # noqa: BLE001
                    it.warnings.append(f"DB取込の有無を確認できません: {type(exc).__name__}")
                if it.db_rows == 0 and e.memo.strip():
                    it.warnings.append("DBへ未取込です（取込が必要な対象。無関係な取込・大量ダウンロードは行わず報告のみ）")
            it.warnings.append("DBへのASIN取込は元CSVの全列の保全ではありません（保全はローカルの元CSVで判断）")
        elif e.status in ("待機中", "取得中"):
            it.blockers.append(f"{e.status}です（削除されると出力を失う可能性があるため、完了するまで新規送信を止めます）")
        elif e.status == "禁止セラー":
            it.warnings.append("出力が無い行（記録のみ）")
        else:
            it.blockers.append(f"状態{e.status!r}を判定できません（詳細が不明のため、安全のため新規送信を止めます）")
        out.append(it)
    return out


def blockers_of(items: list[RetentionItem]) -> list[str]:
    return [f"ログ{it.seq}（{it.memo or 'メモなし'}・{it.status}）: {b}" for it in items for b in it.blockers]


def record_retention(path: Path, items: list[RetentionItem], *, context: dict | None = None) -> int:
    """削除される可能性のある行の記録（行が消えた後も追えるように、ローカルのJSONLへ追記）。"""
    if not items:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for it in items:
            fh.write(json.dumps({"ts": ts, **(context or {}), **asdict(it), "ok": it.ok}, ensure_ascii=False) + "\n")
    return len(items)
