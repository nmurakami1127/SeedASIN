"""人の判断が要る未完了（D区分）の運用設計（docs/DEVIATIONS.md #98）。

D（人手が必要）を、未完了のworkflowとして**無期限に残さない**ための、追記専用の台帳 `<state_dir>/needs_human.jsonl`。
- 取り分け(QUARANTINE): Dの投入工程のうち、台帳が追跡していない・人の判断待ちが確定している理由（`QUARANTINABLE`）だけを、
  `needs_human.jsonl`に1キー1件で記録し、workflowの工程は `closed_reason="NEEDS_HUMAN"` で閉じる（状態の直書きCOMPLETEDではない。
  投入台帳・ASIN・取得履歴は変更しない）。**登録済みとは扱わない**: 一覧(`needs-human`)・outcome・watchの期限切れ通知に出続ける。
- 期限(review_due): 取り分けた日から `REVIEW_DAYS` 日。期限を過ぎて未解決ならwatchが通知する（`notify_minutes`ごと）。
- 解決(RESOLVED): 人が決めた結果（ALLOWED=意図した再投入として許可 / ALREADY_REGISTERED=登録済みと確認 / WITHDRAWN=取り下げ / OTHER）とメモを記録。
- 戻し(REOPENED): 取り分けを取り消して工程を元の状態に戻し、通常の再開に戻す（ALLOWEDの場合は`--allow-duplicate`相当の許可も状態に追記）。
取り分けない理由（結果不明・照合中・CAPTCHA・人の操作待ち・状態ファイル破損）は、進行中の仕組みが追跡しているため、未完了のまま見えるように残す。

イベント: NOTED / WORKFLOW_ADDED（同じキーが別のworkflowにも現れた）/ RESOLVED / REOPENED
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REVIEW_DAYS = 7
FILE_NAME = "needs_human.jsonl"
# 取り分けてよい理由（人の判断待ちが確定していて、台帳の状態機械が追跡していないもの）
QUARANTINABLE = ("REPEAT_OF_EARLIER_CYCLE", "LEDGER_WITHDRAWN", "LEDGER_ABANDONED", "NO_LEDGER_KEY")
DECISIONS = ("ALLOWED", "ALREADY_REGISTERED", "WITHDRAWN", "OTHER")


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def item_id(kind: str, key: str) -> str:
    return "nh-" + hashlib.sha1(f"{kind}|{key}".encode()).hexdigest()[:10]


@dataclass
class Item:
    id: str
    kind: str
    key: str
    reason: str
    noted_at: str
    review_due: str
    workflows: list = field(default_factory=list)   # [{workflow, task, from_status}]
    info: dict = field(default_factory=dict)
    resolved: dict | None = None
    reopened: bool = False


class NeedsHuman:
    def __init__(self, path: str | Path, now=None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def append(self, event: str, id_: str, **data) -> dict:
        rec = {"ts": iso(self.now()), "event": event, "id": id_, **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec

    def events(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        return out

    def items(self) -> dict[str, Item]:
        its: dict[str, Item] = {}
        for e in self.events():
            i = e["id"]
            if e["event"] == "NOTED":
                its[i] = Item(i, e["kind"], e["key"], e["reason"], e["ts"], e["review_due"], list(e.get("workflows") or []), e.get("info") or {})
            elif i in its:
                it = its[i]
                if e["event"] == "WORKFLOW_ADDED":
                    it.workflows += [w for w in (e.get("workflows") or []) if w not in it.workflows]
                elif e["event"] == "RESOLVED":
                    it.resolved = {"decision": e.get("decision"), "note": e.get("note"), "ts": e["ts"]}
                elif e["event"] == "REOPENED":
                    it.reopened, it.resolved = True, {"decision": "REOPENED", "note": e.get("note"), "ts": e["ts"]}
        return its

    def open_items(self) -> list[Item]:
        return [i for i in self.items().values() if i.resolved is None]

    def overdue(self) -> list[Item]:
        now = iso(self.now())
        return [i for i in self.open_items() if i.review_due <= now]

    def note(self, kind: str, key: str, reason: str, workflows: list, info: dict) -> tuple[Item, bool]:
        """取り分けを記録する（冪等）。同じキーが既に未解決なら、workflowだけ追加する。戻り値は (項目, 新規か)。"""
        i = item_id(kind, key)
        cur = self.items().get(i)
        if cur and cur.resolved is None:
            new = [w for w in workflows if w not in cur.workflows]
            if new:
                self.append("WORKFLOW_ADDED", i, workflows=new)
            return self.items()[i], False
        self.append("NOTED", i, kind=kind, key=key, reason=reason, review_due=iso(self.now() + timedelta(days=REVIEW_DAYS)), workflows=workflows, info=info)
        return self.items()[i], True

    def resolve(self, id_: str, decision: str, note: str) -> dict:
        if decision not in DECISIONS:
            raise ValueError(f"decision は {DECISIONS} から指定してください")
        it = self.items().get(id_)
        if it is None or it.resolved is not None:
            raise KeyError(f"未解決の項目がありません: {id_}")
        return self.append("RESOLVED", id_, decision=decision, note=note)


def format_items(items: list[Item], now: datetime) -> list[str]:
    if not items:
        return ["人の判断待ち(needs-human): 0件"]
    out = [f"人の判断待ち(needs-human): {len(items)}件（期限切れ{len([i for i in items if parse(i.review_due) <= now])}件）"]
    for i in sorted(items, key=lambda x: x.review_due):
        late = "【期限切れ】" if parse(i.review_due) <= now else ""
        out.append(f"  {i.id} {late}期限{i.review_due} {i.kind} {i.reason} key={i.key[:40]} workflow{len(i.workflows)}件 {i.info.get('summary', '')}")
    return out
