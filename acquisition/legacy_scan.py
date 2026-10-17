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
from dataclasses import dataclass, field
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
    run_status: str | None = None  # 旧collector_runのstatus（RUNNING/CAPTCHA_WAITの判別用）
    flags: dict = field(default_factory=dict)  # 誤分類候補の裏付け/矛盾の確認用（enrich_heuristics）

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
        # 商品ページの調査記録(run_type='ASIN_PAGE'、acquisition.discovery)は旧収集の履歴ではないので除く
        where = " WHERE source_id IS NULL AND run_type <> 'ASIN_PAGE'" if _has_column(conn, "collector_run", "source_id") else ""
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
                note=f"status={r.status} run_type={r.run_type}", recorded_source=r.run_type, recorded_at=r.started_at, run_status=r.status,
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
    "asin_counts", "within_30d", "suggested_type", "suggested_key", "suggestion_basis",
    "decided_type", "decided_key", "decided_qualifier", "decided_note",
]
UNCLASSIFIED_DETAIL_COLUMNS = [
    "no", "group", "kind", "original_value", "recorded_source", "ref", "ref_detail", "data_market", "evidence_at_utc",
    "evidence_at_basis", "requested_or_started_at_utc", "asin_count", "success_grade", "within_30d",
]


def suggest_from_url(raw: str) -> tuple[str, str, str] | None:
    """URLの**明示的なクエリパラメータ**から客観的に言える分類案 (種別, キー, 根拠)。案であり、DBには書かない。
    me=<セラーID> → SELLER / rh=...p_89:<ブランド名>（ブランド絞り込み）→ BRAND_NAME。
    k= (検索語) は、同じURLにブランド絞り込みがあれば案にしない（キーワードかブランドか決められないため）。
    名前や文字列の印象からは案を作らない。"""
    from urllib.parse import parse_qs, unquote, urlparse

    if not raw.lower().startswith(("http://", "https://")):
        return None
    q = parse_qs(urlparse(raw).query)
    me = (q.get("me") or [""])[0].strip()
    if me:
        return "SELLER", me.upper(), "URLのme=（出品者指定）"
    brands = []
    for rh in q.get("rh", []):
        for part in unquote(rh).split(","):
            if part.startswith("p_89:"):
                brands += [b for b in part[len("p_89:"):].split("|") if b]
    brands = sorted(set(brands))
    if len(brands) == 1:
        return "BRAND_NAME", brands[0].replace("+", " "), "URLのrh=p_89（ブランド絞り込み）"
    return None


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


def _suggestion_cells(group: list[Observation]) -> tuple[str, str, str]:
    sugg = {suggest_from_url(o.raw_value) for o in group}
    if len(sugg) == 1 and None not in sugg:
        t, k, b = next(iter(sugg))
        return t, k, b
    return "", "", ""


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
                *_suggestion_cells(g),
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


# ---------------------------------------------------------------------- 移行対象の選択（legacy-select）
#
# 選択条件（すべてを満たす観測だけを選ぶ。基準時刻はDB時刻）:
#   (1) 種別・出典キーを**確認できる**（type_grade=FACT: 記録されたdata-typeやURLの me=/k= による。
#       セラーID形式の正規表現に一致するだけの推定は「確認」に含めず、誤分類候補として別の一覧に分ける）
#   (2) 証拠時刻が、その出典の取得間隔内（as_of < 証拠時刻 + 間隔）。間隔は acquisition_interval_config から。
#       取得先市場が不明な証拠は、両市場の間隔の長い方
#   (3) 取得成功について承認済みの参考証拠がある（success_grade=ESTIMATE）
# 選択しないもの: 未分類文字列、done/、取得成功の根拠がないもの、旧RUNNING/CAPTCHA_WAIT。
#
# 扱い:
#   - 取得先市場が不明: US・JP双方のREVIEW_LEGACY用として選択（SELECT_BOTH）。成功も取得先市場も確定しない。
#   - 取得先市場が記録(URLドメイン)で分かる: 旧方式の取得成功は「参考証拠」として保持（SELECT_RECORDED_MARKET。
#     取得先市場だけを記録に基づき確定し、成功日時の断定はしない）。
# 除外理由は、次の順で最初に該当したものを1つだけ付ける（件数が重複しない）。

EXCLUSION_ORDER = [
    ("LEGACY_RUNNING", "旧RUNNING/CAPTCHA_WAIT（変更も証拠化もしない）"),
    ("NO_SUCCESS_BASIS", "取得成功の根拠がない（取込なし・件数0・失敗・NGを含む日など）"),
    ("OUTSIDE_INTERVAL", "証拠時刻が取得間隔の外（履歴としては今回選択しない）"),
    ("TYPE_HEURISTIC_ONLY", "種別がID形式等の推定のみ（誤分類候補として別の一覧）"),
    ("TYPE_UNCLASSIFIED", "種別を確認できない（未分類）"),
    ("KEY_UNUSABLE", "種別は確認できるが出典キーを生成できない"),
]
REASON_LABEL = dict(EXCLUSION_ORDER)


@dataclass
class SelectionRow:
    obs: Observation
    decision: str  # SELECT_BOTH / SELECT_RECORDED_MARKET / EXCLUDE
    reason: str | None
    interval_days: int
    key: tuple[str, str] | None

    @property
    def selected(self) -> bool:
        return self.decision != "EXCLUDE"

    @property
    def planned_rows(self) -> int:
        # SELECT_BOTH: 観測×2区画 + CLASSIFY×2区画。SELECT_RECORDED_MARKET: 観測 + CLASSIFY + CONFIRM（1区画）
        return {"SELECT_BOTH": 4, "SELECT_RECORDED_MARKET": 3}.get(self.decision, 0)


def select_for_migration(observations: list[Observation], interval_days, as_of: datetime) -> list[SelectionRow]:
    """interval_days(source_type|None, market|None) -> 日数。as_of=DB時刻。"""
    out: list[SelectionRow] = []
    for o in observations:
        known_market = o.market_grade == "FACT" and o.market_hint in ("US", "JP")
        days = interval_days(o.type_hint, o.market_hint if known_market else None)
        key = o.proposed_key() if o.type_grade == "FACT" else None
        reason = None
        if o.run_status in ("RUNNING", "CAPTCHA_WAIT"):
            reason = "LEGACY_RUNNING"
        elif o.success_grade != "ESTIMATE":
            reason = "NO_SUCCESS_BASIS"
        elif not (as_of < o.evidence_at + timedelta(days=days)):
            reason = "OUTSIDE_INTERVAL"
        elif o.type_hint and o.type_grade != "FACT":
            reason = "TYPE_HEURISTIC_ONLY"
        elif not o.type_hint:
            reason = "TYPE_UNCLASSIFIED"
        elif key is None:
            reason = "KEY_UNUSABLE"
        decision = "EXCLUDE" if reason else ("SELECT_RECORDED_MARKET" if known_market else "SELECT_BOTH")
        out.append(SelectionRow(o, decision, reason, days, key))
    return out


def summarize_selection(rows: list[SelectionRow]) -> dict:
    """証拠数・出典数・予定行数を区別して集計する。"""
    from collections import Counter

    sel = [r for r in rows if r.selected]
    both = [r for r in sel if r.decision == "SELECT_BOTH"]
    rec = [r for r in sel if r.decision == "SELECT_RECORDED_MARKET"]
    src = lambda rs: {(r.obs.type_hint, r.key) for r in rs}
    held_both = src(both)
    return {
        "observations_total": len(rows),
        "selected_observations": len(sel),
        "selected_by_kind": dict(Counter(r.obs.kind for r in sel)),
        "selected_by_decision": dict(Counter(r.decision for r in sel)),
        "sources_total": len(src(sel)),
        "sources_held_in_both_markets": len(held_both),
        "sources_confirmed_by_market": {m: len(src([r for r in rec if r.obs.market_hint == m])) for m in ("US", "JP")},
        "sources_with_both_kinds": len(held_both & src(rec)),
        "planned_rows": sum(r.planned_rows for r in sel),
        "planned_rows_breakdown": {
            "観測(両区画に複製)": 2 * len(both), "観測(記録の市場の1区画)": len(rec),
            "CLASSIFY判断": 2 * len(both) + len(rec), "CONFIRM判断(記録の市場)": len(rec),
        },
        "excluded_by_reason": {code: sum(1 for r in rows if r.reason == code) for code, _ in EXCLUSION_ORDER},
        "excluded_total": len(rows) - len(sel),
        "excluded_by_reason_and_kind": {
            code: dict(Counter(r.obs.kind for r in rows if r.reason == code)) for code, _ in EXCLUSION_ORDER
        },
    }


SELECTION_EXTRA_COLUMNS = ["selection", "exclusion_reason", "interval_days", "source_type", "source_key", "source_qualifier",
                           "planned_db_rows"]


OBSERVE_ONLY_REASONS = ("TYPE_HEURISTIC_ONLY", "TYPE_UNCLASSIFIED")


def write_selection_csv(rows: list[SelectionRow], path: Path, as_of: datetime, *, observe_only: bool = False) -> Path:
    """選択結果。元のdry-run CSVと同じ列に、選択の結果を足した別ファイル。apply列は選択した行だけY。
    observe_only=True: 種別が確認できない履歴（推定のみ/未分類。取得成功の根拠と取得間隔内は満たす）だけにYを付ける。
    legacy-apply を --classify none で使い、観測として残すだけ（種別・出典は作らず、保留にもしない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(DRY_RUN_COLUMNS + SELECTION_EXTRA_COLUMNS)
        for r in rows:
            o = r.obs
            at = o.evidence_at.astimezone(timezone.utc)
            w.writerow([
                "Y" if ((r.reason in OBSERVE_ONLY_REASONS) if observe_only else r.selected) else "", o.group, o.kind, o.ref, o.data_marketplace, o.raw_value, at.strftime("%Y-%m-%d %H:%M:%S"),
                o.evidence_at_basis, "" if o.asin_count is None else o.asin_count,
                f"{o.success_grade}({GRADE_LABEL[o.success_grade]})", f"{o.market_grade}({GRADE_LABEL[o.market_grade]})",
                f"{o.type_grade}({GRADE_LABEL[o.type_grade]})", o.type_hint or "", o.proposed_primary or "",
                o.proposed_qualifier, o.type_hint_basis or "", o.market_hint or "", o.market_hint_basis or "",
                "Y" if as_of - at <= timedelta(days=30) else "", proposed_action(o), o.note,
                r.decision if r.selected else "EXCLUDE", REASON_LABEL.get(r.reason, "") if r.reason else "", r.interval_days,
                r.key and o.type_hint or "", r.key[0] if r.key else "", r.key[1] if r.key else "", r.planned_rows,
            ])
    return path


# ---- 誤分類候補（セラーID形式の推定のみ）の一覧

HEURISTIC_COLUMNS = [
    "group", "market_or_direction", "raw_value", "ref", "evidence_at_utc", "asin_count", "recorded_source", "length",
    "asin_like_length_10", "in_known_seller_master_same_market", "also_recorded_as_seller_page_or_competitor",
    "recorded_as_keyword_or_brand_elsewhere", "run_count_for_value", "risk_label", "note",
]


def enrich_heuristics(app_factory: SessionFactory, observations: list[Observation]) -> None:
    """ID形式の推定分類(type_grade=ESTIMATEのSELLER)に、誤分類の裏付け/矛盾を示す事実を付ける（読み取り専用）。
    裏付けがあっても確認済み種別にはしない: 一覧で人が判断するための材料。"""
    for market in ("US", "JP"):
        with app_factory.get_session(market) as conn, conn.begin():
            _set_read_only(conn)
            ksm = {r[0].strip().upper() for r in conn.execute(text("SELECT seller_id FROM known_seller_master"))}
            tel: dict[str, list[str | None]] = {}
            for sid, src in conn.execute(text("SELECT seller_id, source FROM tool_execution_link")):
                tel.setdefault(sid.strip().upper(), []).append((src or "").strip().upper() or None)
        for o in observations:
            if o.kind != "TOOL_RUN" or o.data_marketplace != market or o.type_hint != "SELLER" or o.type_grade == "FACT":
                continue
            v = o.raw_value.strip().upper()
            srcs = tel.get(v, [])
            o.flags = {
                "length": len(v), "asin_like": len(v) == 10,
                "in_ksm": v in ksm,
                "seller_page": any(s in ("SELLER_PAGE", "COMPETITOR") for s in srcs),
                "keyword_brand": any(s in ("KEYWORD", "KEYWROD", "BRAND", "BRAND_STORE") for s in srcs),
                "runs": len(srcs),
            }


def heuristic_risk_label(o: Observation) -> str:
    f = o.flags
    if f.get("keyword_brand"):
        return "矛盾あり（別の実行ではキーワード/ブランドとして記録）"
    if f.get("asin_like"):
        return "要確認（ASINと同じ10桁）"
    if f.get("in_ksm") or f.get("seller_page"):
        return "裏付けあり（セラー台帳に登録済み/セラー系の記録あり。ただし確認済み種別ではない）"
    return "裏付けなし（ID形式の一致のみ）"


def write_heuristic_csv(rows: list[SelectionRow], path: Path) -> tuple[Path, dict]:
    """推定のみ(TYPE_HEURISTIC_ONLY)で除外した観測を、誤分類の確認材料つきで出力する。選択はしない。"""
    from collections import Counter

    cands = [r.obs for r in rows if r.reason == "TYPE_HEURISTIC_ONLY"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = lambda ts: ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEURISTIC_COLUMNS)
        for o in sorted(cands, key=lambda x: (heuristic_risk_label(x), x.raw_value, x.ref)):
            f = o.flags
            w.writerow([o.group, o.data_marketplace, o.raw_value, o.ref, fmt(o.evidence_at), o.asin_count, o.recorded_source or "(記録なし)",
                        f.get("length", ""), "Y" if f.get("asin_like") else "", "Y" if f.get("in_ksm") else "",
                        "Y" if f.get("seller_page") else "", "Y" if f.get("keyword_brand") else "", f.get("runs", ""),
                        heuristic_risk_label(o), o.type_hint_basis or ""])
    labels = Counter(heuristic_risk_label(o) for o in cands)
    distinct = {}
    for lab in labels:
        distinct[lab] = len({o.raw_value.strip().upper() for o in cands if heuristic_risk_label(o) == lab})
    return path, {"observations": len(cands), "distinct_values": len({o.raw_value.strip().upper() for o in cands}),
                  "by_label": dict(labels), "distinct_by_label": distinct}


# ---- セラー台帳(Excel)の「seller id」列による種別の確認
#
# ID形式(正規表現)の一致だけではSELLERを確認済みにしない。ただし、セラー台帳の「seller id」列に
# （Brand印の行ではなく）そのIDが登録されていて、かつ別の実行でキーワード/ブランドとして使われた記録がなく、
# ASINと同じ10桁でもなければ、「そのIDがセラーとして扱われた」明示的な根拠とみなして種別を確認済み(FACT)にする。
# 取得先市場と取得成功の扱いは変えない（市場不明は両市場REVIEW_LEGACYのまま）。

LEDGER_BASIS = "セラー台帳のseller id列に登録(Brand行でない・矛盾記録なし)"  # 列長60以内


def load_ledger_seller_ids(path: Path, sheet: str) -> set[str]:
    """台帳ワークブックの「seller id」列（4列目）から、Brand印でない行のIDを集める（読み取り専用）。"""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ids: set[str] = set()
        for i, row in enumerate(wb[sheet].iter_rows(values_only=True)):
            if i == 0:
                continue
            name = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
            sid = str(row[3]).strip() if len(row) > 3 and row[3] is not None else ""
            if sid and sid != "Brand" and name != "Brand":
                ids.add(sid.upper())
        return ids
    finally:
        wb.close()


def apply_ledger_corroboration(observations: list[Observation], ledger_ids: dict[str, set[str]]) -> int:
    """ID形式の推定SELLER(TOOL_RUN)のうち、台帳の根拠があり矛盾がないものを確認済み種別にする。昇格した件数を返す。
    enrich_heuristics の後に呼ぶ（flags が必要）。"""
    n = 0
    for o in observations:
        if o.kind != "TOOL_RUN" or o.type_hint != "SELLER" or o.type_grade == "FACT":
            continue
        f = o.flags
        if not f or f.get("keyword_brand") or f.get("asin_like"):
            continue
        if o.raw_value.strip().upper() in ledger_ids.get(o.data_marketplace, set()):
            o.type_grade = "FACT"
            o.type_hint_basis = LEDGER_BASIS
            n += 1
    return n
