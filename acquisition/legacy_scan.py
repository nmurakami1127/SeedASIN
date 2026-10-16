"""旧履歴の読み取り（dry-run）。DBへの書き込み・外部アクセスは一切しない。

各証拠について、**確認できた事実・推定・未確認**を分けて出力する（success / market / type の3軸）:

| 証拠 | success（取得が成功したか） | market（取得先市場） | type（出典の種別） |
|---|---|---|---|
| 統合ツール実行 + 取込済みASIN | 推定: MAX(tool_output_asin.acquired_at) は「ツールがデータを得た時刻」で出典の取得成功日時とは断定しない。取込なしは未確認 | **未確認**（tel.marketplaceは業務方向。取得先ではない） | 記録(data-type)が KEYWORD/SELLER_PAGE/COMPETITOR は事実、ID形式からのSELLERは推定、他は未確認 |
| 旧collector_run | 推定: 旧コレクタは正常0件と失敗を区別できないため COMPLETED+件数ありでも断定しない。0件・失敗・実行中は未確認 | **事実**: URLのドメイン（marketplace_from_url） | URLの me= → SELLER、k= → KEYWORD は事実。他は未確認 |
| セラー側 status.csv | 推定: 同日にNG・中断が無いOKの日。価格帯分割の網羅は断定しない | **事実**: 記録されたURLのドメイン | URLの me= → SELLER は事実 |

旧RUNNING行（id 33・38）を含む旧経路の行は、証拠にも回収にも使わない: 件数のみ別に報告する。
done/ は一覧のみ（証拠にはしない）。
"""
from __future__ import annotations

import csv
import glob
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

from sqlalchemy import Connection, text

from acquisition.normalize import normalize_key
from db.session import SessionFactory

_NS = uuid.UUID("6f0d1c1e-3a52-4c0e-9f43-0d7a3b6a5e11")
_ID_FORM = re.compile(r"^A[0-9A-Z]{9,15}$")
JST = timezone(timedelta(hours=9))

DEFAULT_SELLER_OUTPUT_ROOT = Path(r"C:\work\output\amazon_seller_asins")
DEFAULT_DONE_DIR = Path(r"C:\work\pgm\amazon_us_asin_only_automator\done")


def group_id(kind: str, ref: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}|{ref}"))


@dataclass
class Observation:
    group: str
    kind: str
    ref: str
    data_marketplace: str
    raw_value: str
    evidence_at: datetime
    evidence_at_basis: str
    asin_count: int | None
    success_grade: str
    market_grade: str
    type_grade: str
    market_hint: str | None
    market_hint_basis: str | None
    type_hint: str | None
    type_hint_basis: str | None
    proposed_primary: str | None = None
    proposed_qualifier: str = ""
    note: str = ""
    # 確認用（未分類一覧など）。記録されていた元の値のみで、推測はしない
    recorded_source: str | None = None  # tool_execution_link.source（ログのdata-type）。無ければNone
    recorded_at: datetime | None = None  # requested_at（依頼日）またはcollector_runの開始日時
    ref_detail: str = ""  # 参照元の補足（run_type/status/URL等）

    def proposed_key(self) -> tuple[str, str] | None:
        if not self.type_hint or not self.proposed_primary:
            return None
        try:
            return normalize_key(self.type_hint, self.proposed_primary, self.proposed_qualifier)
        except ValueError:
            return None


def _type_from_record(source: str | None, seller_id: str) -> tuple[str | None, str, str | None]:
    """tool_execution_link.source(ログのdata-type)と文字列形から (type, grade, basis)。"""
    s = (source or "").strip().upper()
    id_form = bool(_ID_FORM.match(seller_id.strip().upper()))
    if s in ("KEYWORD", "KEYWROD"):
        return "KEYWORD", "FACT", "記録(data-type=KEYWORD)"
    if s in ("SELLER_PAGE", "COMPETITOR") and id_form:
        return "SELLER", "FACT", f"記録(data-type={s})かつID形式"
    if s == "BRAND":
        return "BRAND_NAME", "ESTIMATE", "記録(data-type=BRAND)。名前検索かストアかは不明のためBRAND_NAMEと推定"
    if id_form:
        return "SELLER", "ESTIMATE", "ヒューリスティック: セラーID形式(A+英数字9〜15桁)"
    return None, "UNCONFIRMED", None


def _set_read_only(conn: Connection) -> None:
    conn.execute(text("SET TRANSACTION READ ONLY"))


def collect_tool_runs(app_factory: SessionFactory, marketplace: str) -> list[Observation]:
    out: list[Observation] = []
    with app_factory.get_session(marketplace) as conn, conn.begin():
        _set_read_only(conn)
        rows = conn.execute(
            text(
                "WITH o AS (SELECT tool_run_id, count(*) n, max(acquired_at) mx FROM tool_output_asin GROUP BY 1) "
                "SELECT t.tool_run_id, t.seller_id, t.source, t.requested_at, o.n, o.mx "
                "FROM tool_execution_link t LEFT JOIN o ON o.tool_run_id = t.tool_run_id ORDER BY t.id"
            )
        ).all()
    for r in rows:
        ref = f"{marketplace}:{r.tool_run_id}"
        typ, tgrade, tbasis = _type_from_record(r.source, r.seller_id)
        has_output = r.n is not None and r.n > 0
        out.append(
            Observation(
                group=group_id("TOOL_RUN", ref), kind="TOOL_RUN", ref=ref, data_marketplace=marketplace,
                raw_value=r.seller_id, evidence_at=r.mx if has_output else r.requested_at,
                evidence_at_basis="MAX(tool_output_asin.acquired_at)（参考。取得成功日時とは断定しない）" if has_output
                else "requested_at（取込なし。証拠にしない）",
                asin_count=int(r.n) if r.n is not None else 0,
                success_grade="ESTIMATE" if has_output else "UNCONFIRMED", market_grade="UNCONFIRMED", type_grade=tgrade,
                market_hint=None, market_hint_basis="取得先市場は不明（業務方向=" + marketplace + "）",
                type_hint=typ, type_hint_basis=tbasis, proposed_primary=r.seller_id if typ else None,
                note=f"source={r.source or '(null)'}", recorded_source=r.source, recorded_at=r.requested_at,
                ref_detail=f"tool_run_id={r.tool_run_id} 業務方向={marketplace}",
            )
        )
    return out


def _domain_market(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("amazon.co.jp"):
        return "JP"
    if host.endswith("amazon.com"):
        return "US"
    return None


def _key_from_url(url: str) -> tuple[str | None, str | None, str]:
    """URLから (種別, 主キー, 修飾キー)。me= → SELLER、k= → KEYWORD（i=はカテゴリ）。"""
    q = parse_qs(urlparse(url).query)
    if q.get("me"):
        return "SELLER", unquote_plus(q["me"][0]), ""
    if q.get("k"):
        cat = q.get("i", [""])[0]
        return "KEYWORD", unquote_plus(q["k"][0]), "" if cat == "merchant-items" else cat
    return None, None, ""


def _has_column(conn: Connection, table: str, column: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).first() is not None


def collect_collector_runs(app_factory: SessionFactory, marketplace: str) -> tuple[list[Observation], dict[str, int]]:
    out: list[Observation] = []
    stats = {"legacy_running_untouched": 0, "total": 0}
    with app_factory.get_session(marketplace) as conn, conn.begin():
        _set_read_only(conn)
        where = " WHERE source_id IS NULL" if _has_column(conn, "collector_run", "source_id") else ""
        rows = conn.execute(
            text(
                "SELECT id, run_type, target, status, asin_count, started_at, completed_at "
                f"FROM collector_run{where} ORDER BY id"
            )
        ).all()
    for r in rows:
        stats["total"] += 1
        if r.status in ("RUNNING", "CAPTCHA_WAIT"):
            stats["legacy_running_untouched"] += 1
        typ, primary, qual = _key_from_url(r.target)
        market = _domain_market(r.target)
        nonempty_done = r.status == "COMPLETED" and (r.asin_count or 0) > 0
        out.append(
            Observation(
                group=group_id("COLLECTOR_RUN", f"{marketplace}:{r.id}"), kind="COLLECTOR_RUN",
                ref=f"{marketplace}:{r.id}", data_marketplace=marketplace, raw_value=primary or r.target,
                evidence_at=r.completed_at or r.started_at,
                evidence_at_basis="completed_at（旧コレクタ。正常0件と失敗を区別できない）" if r.completed_at
                else "started_at（未完了。証拠にしない）",
                asin_count=r.asin_count,
                success_grade="ESTIMATE" if nonempty_done else "UNCONFIRMED",
                market_grade="FACT" if market == marketplace else "UNCONFIRMED",
                type_grade="FACT" if typ else "UNCONFIRMED",
                market_hint=market, market_hint_basis="URLドメイン(marketplace_from_url)" if market else None,
                type_hint=typ, type_hint_basis=("URLの" + ("me=" if typ == "SELLER" else "k=")) if typ else None,
                proposed_primary=primary, proposed_qualifier=qual,
                note=f"status={r.status} run_type={r.run_type}", recorded_source=r.run_type, recorded_at=r.started_at,
                ref_detail=f"collector_run id={r.id} status={r.status} URL={r.target}",
            )
        )
    return out, stats


def collect_status_csv(root: Path) -> list[Observation]:
    """セラー側 _amazoncom_batches/*/status.csv。セラーごとに「OKのある最新の日」を1件。"""
    per: dict[str, dict[str, dict]] = {}
    for f in glob.glob(os.path.join(str(root), "_amazoncom_batches", "batch_*", "status.csv")):
        day = re.search(r"batch_(\d{8})", f).group(1)
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    url = r.get("url") or ""
                    m = re.search(r"[?&]me=([A-Z0-9]+)", url)
                    if not m:
                        continue
                    d = per.setdefault(m.group(1), {}).setdefault(
                        day, {"OK": 0, "NG": 0, "INT": 0, "last": None, "market": _domain_market(url), "file": f}
                    )
                    st = (r.get("status") or "").strip()
                    key = {"OK": "OK", "NG": "NG", "INTERRUPTED": "INT"}.get(st)
                    if key:
                        d[key] += 1
                    t = (r.get("time") or "").strip()
                    if st == "OK" and t:
                        try:
                            ts = datetime.fromisoformat(t).replace(tzinfo=JST)  # ローカル時刻をJSTと仮定
                            d["last"] = max(d["last"], ts) if d["last"] else ts
                        except ValueError:
                            pass
        except OSError:
            continue
    out: list[Observation] = []
    for seller, days in per.items():
        ok_days = sorted(d for d, v in days.items() if v["OK"] > 0)
        if not ok_days:
            continue
        day = ok_days[-1]
        v = days[day]
        full = v["NG"] == 0 and v["INT"] == 0
        at = v["last"] or datetime.strptime(day, "%Y%m%d").replace(tzinfo=JST)
        market = v["market"]
        out.append(
            Observation(
                group=group_id("STATUS_CSV", f"{day}:{seller}"), kind="STATUS_CSV", ref=f"{day}:{seller}",
                data_marketplace=market or "US", raw_value=seller, evidence_at=at,
                evidence_at_basis="status.csv最終OK時刻（ローカル時刻をJSTと仮定）" if v["last"] else "バッチ日付",
                asin_count=None, success_grade="ESTIMATE" if full else "UNCONFIRMED",
                market_grade="FACT" if market else "UNCONFIRMED", type_grade="FACT",
                market_hint=market, market_hint_basis="status.csvに記録されたURLのドメイン",
                type_hint="SELLER", type_hint_basis="URLのme=", proposed_primary=seller,
                note=f"batch={day} OK={v['OK']} NG={v['NG']} 中断={v['INT']}（価格帯分割の網羅は断定しない）"
                + ("" if full else "／NGまたは中断を含む日のため未確認"),
            )
        )
    return out


def scan_done_dir(path: Path) -> dict:
    """done/ は一覧のみ（証拠にしない）。キーワード側の日付付きファイルの要約。"""
    files = sorted(glob.glob(os.path.join(str(path), "*keywords_us.csv")))
    days = sorted({os.path.basename(f)[:8] for f in files})
    keys: dict[tuple[str, str], list[str]] = {}
    rows = 0
    for f in files:
        day = os.path.basename(f)[:8]
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                data = list(csv.reader(fh))
        except OSError:
            continue
        body = data[1:] if data and data[0] and data[0][-1].strip().lower() == "keyword" else data
        for r in body:
            if not r:
                continue
            keys.setdefault((r[0].strip().lower() if len(r) > 1 else "", r[-1].strip().lower()), []).append(day)
            rows += 1
    return {
        "files": len(files), "days": days, "keyword_rows": rows, "distinct": len(keys),
        "repeated": sum(1 for v in keys.values() if len(v) > 1),
        "note": "一覧のみ。取得先はコード上amazon.com(US)だが記録が無く、完了の確認も無いため証拠にしない",
    }


GRADE_LABEL = {"FACT": "事実", "ESTIMATE": "推定", "UNCONFIRMED": "未確認"}


def proposed_action(o: Observation) -> str:
    if o.success_grade == "UNCONFIRMED":
        return "SKIP（取得成功が未確認）"
    parts = ["OBSERVE"]
    if o.proposed_key():
        parts.append("CLASSIFY")
    parts.append("CONFIRM_MARKET(記録)" if o.market_grade == "FACT" else "BOTH_MARKETS(取得先市場は未確認)")
    return "+".join(parts)


DRY_RUN_COLUMNS = [
    "apply", "group", "kind", "ref", "data_market", "raw_value", "evidence_at_utc", "evidence_at_basis", "asin_count",
    "success_grade", "market_grade", "type_grade", "proposed_type", "proposed_primary", "proposed_qualifier",
    "type_basis", "market_hint", "market_basis", "within_30d", "proposed_action", "note",
]


def write_dry_run_csv(observations: list[Observation], path: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(DRY_RUN_COLUMNS)
        for o in observations:
            at = o.evidence_at.astimezone(timezone.utc)
            w.writerow([
                "", o.group, o.kind, o.ref, o.data_marketplace, o.raw_value, at.strftime("%Y-%m-%d %H:%M:%S"),
                o.evidence_at_basis, "" if o.asin_count is None else o.asin_count,
                f"{o.success_grade}({GRADE_LABEL[o.success_grade]})", f"{o.market_grade}({GRADE_LABEL[o.market_grade]})",
                f"{o.type_grade}({GRADE_LABEL[o.type_grade]})", o.type_hint or "", o.proposed_primary or "",
                o.proposed_qualifier, o.type_hint_basis or "", o.market_hint or "", o.market_hint_basis or "",
                "Y" if now - at <= timedelta(days=30) else "", proposed_action(o), o.note,
            ])
    return path


def summarize(observations: list[Observation], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    from collections import Counter

    c: Counter = Counter()
    for o in observations:
        recent = now - o.evidence_at.astimezone(timezone.utc) <= timedelta(days=30)
        c[(o.kind, f"success={o.success_grade}", f"market={o.market_grade}", f"type={o.type_grade}", "30日以内" if recent else "30日超")] += 1
    return dict(c)


# ---------------------------------------------------------------------- 未分類の確認用一覧

UNCLASSIFIED_SUMMARY_COLUMNS = [
    "no", "original_values", "n_observations", "recorded_types", "business_directions_or_markets", "refs",
    "evidence_at_first_utc", "evidence_at_last_utc", "requested_at_first_utc", "requested_at_last_utc",
    "asin_counts", "within_30d", "decided_type", "decided_key", "decided_qualifier", "decided_note",
]
UNCLASSIFIED_DETAIL_COLUMNS = [
    "no", "group", "kind", "original_value", "recorded_source", "ref", "ref_detail", "data_market", "evidence_at_utc",
    "evidence_at_basis", "requested_or_started_at_utc", "asin_count", "success_grade", "within_30d",
]


def unclassified_review(observations: list[Observation], *, recent_only: bool = True, now: datetime | None = None
                        ) -> list[list[Observation]]:
    """種別を自動では決められなかった（type_hintなし）観測を、表記の揺れ（NFKC・大文字小文字・空白）だけを
    同一視して値ごとにまとめる。**種別の推測はしない**: 元の表記・記録されていたdata-type・参照元・日時を
    並べるだけで、分類は人が決める（decided_*列は空欄）。取得成功が未確認のものは対象外。"""
    from acquisition.normalize import norm_text

    now = now or datetime.now(timezone.utc)
    groups: dict[str, list[Observation]] = {}
    for o in observations:
        if o.type_hint or o.success_grade == "UNCONFIRMED":
            continue
        if recent_only and now - o.evidence_at.astimezone(timezone.utc) > timedelta(days=30):
            continue
        groups.setdefault(norm_text(o.raw_value), []).append(o)
    return [sorted(v, key=lambda o: (o.evidence_at, o.ref)) for _, v in sorted(groups.items())]


def write_unclassified_review(groups: list[list[Observation]], summary_path: Path, detail_path: Path,
                              now: datetime | None = None) -> tuple[Path, Path]:
    now = now or datetime.now(timezone.utc)
    fmt = lambda ts: ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(UNCLASSIFIED_SUMMARY_COLUMNS)
        for i, g in enumerate(groups, start=1):
            at = [o.evidence_at for o in g]
            req = [o.recorded_at for o in g if o.recorded_at]
            w.writerow([
                i, " | ".join(sorted({o.raw_value for o in g})), len(g),
                " | ".join(sorted({o.recorded_source or "(記録なし)" for o in g})),
                " | ".join(sorted({o.data_marketplace for o in g})), " | ".join(o.ref for o in g),
                fmt(min(at)), fmt(max(at)), fmt(min(req)) if req else "", fmt(max(req)) if req else "",
                " | ".join(str(o.asin_count) for o in g),
                "Y" if any(now - o.evidence_at.astimezone(timezone.utc) <= timedelta(days=30) for o in g) else "",
                "", "", "", "",
            ])
    with detail_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(UNCLASSIFIED_DETAIL_COLUMNS)
        for i, g in enumerate(groups, start=1):
            for o in g:
                w.writerow([
                    i, o.group, o.kind, o.raw_value, o.recorded_source or "(記録なし)", o.ref, o.ref_detail, o.data_marketplace,
                    fmt(o.evidence_at), o.evidence_at_basis, fmt(o.recorded_at), "" if o.asin_count is None else o.asin_count,
                    f"{o.success_grade}({GRADE_LABEL[o.success_grade]})",
                    "Y" if now - o.evidence_at.astimezone(timezone.utc) <= timedelta(days=30) else "",
                ])
    return summary_path, detail_path
