"""取得計画（plan）: 除外・保留・実行中・取得間隔を判定してから順位付けする。

順位付けのルール:
1. 使うレポートは import_id で**明示**する（省略不可、コードに固定しない）。各 import が存在し、
   ACTIVE かつ行エラー0件であること。同じ市場の import を2つ指定すると拒否（過去期間を混ぜない）。
   指定していない import 由来の経路は、順位に一切使わない。
2. 実行可能な出典（ELIGIBLE_*）を、未取得(NEVER)と再取得(REFETCH)に分け、未取得を先にする。
3. 各群の中は**代表起点**（指定importに属する経路のうち、注文点数→セッション数→最大獲得率が最良の1件）
   で並べる。複数の起点の実績は合算しない。同点は出典キー順。
4. 既定は**方向別**（輸出=US起点 / 輸入=JP起点 / 起点なし）に順位付けする。同じ出典が両方向から
   発見された場合は両方のセクションに載るが、取得(claim)は出典単位で1回に共有される。
実行不可の出典は順位を付けず、理由付きで別に返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text

from acquisition.eligibility import Decision, SourceFacts, decide, db_now, load_configs, load_facts
from db.session import SessionFactory
from origin_selection.report_import import ReportImportService, ReportInfo

SECTION_ORDER = ("EXPORT", "IMPORT", "ALL", "NO_ORIGIN")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class RepOrigin:
    direction: str
    origin_marketplace: str
    import_id: int
    asin: str
    units: int
    sessions: int
    buybox: object

    def sort_key(self):
        return (-self.units, -self.sessions, -float(self.buybox), self.asin)


@dataclass
class PlanEntry:
    section: str
    group: str  # CONTINUE（途中の続き）/ NEVER / REFETCH
    rank: int
    facts: SourceFacts
    decision: Decision
    rep: RepOrigin | None
    also_in: list[str] = field(default_factory=list)


@dataclass
class Plan:
    target_marketplace: str
    imports: list[ReportInfo]
    as_of: datetime
    sections: dict[str, list[PlanEntry]]
    blocked: list[tuple[SourceFacts, Decision]]
    needs_reaping: list[tuple[SourceFacts, Decision]]


def validate_origin_imports(reports: ReportImportService, import_ids: list[int]) -> list[ReportInfo]:
    if not import_ids:
        raise PlanError("使うレポートを --origin-import で明示してください（過去期間を自動では混ぜません）")
    if len(set(import_ids)) != len(import_ids):
        raise PlanError("同じimport_idが重複しています")
    infos: list[ReportInfo] = []
    for iid in import_ids:
        mp = reports.resolve_market_for_import(iid)
        info = reports.get_report(iid, mp) if mp else None
        if info is None:
            raise PlanError(f"import_id={iid} は存在しません")
        if info.status != "ACTIVE":
            raise PlanError(f"import_id={iid} は有効版（ACTIVE）ではありません（状態: {info.status}）")
        if info.error_rows != 0:
            raise PlanError(f"import_id={iid} は行エラーのあるレポートです")
        infos.append(info)
    markets = [i.marketplace for i in infos]
    if len(set(markets)) != len(markets):
        raise PlanError("同じ市場のimportが複数指定されています（異なる期間を混ぜないため拒否）")
    return infos


def build_plan(
    app_factory: SessionFactory,
    reports: ReportImportService,
    target_marketplace: str,
    origin_import_ids: list[int],
    *,
    group_by_direction: bool = True,
    limit: int | None = None,
    as_of: datetime | None = None,
) -> Plan:
    infos = validate_origin_imports(reports, origin_import_ids)
    with app_factory.get_session(target_marketplace) as conn, conn.begin():
        now = as_of or db_now(conn)
        configs = load_configs(conn, target_marketplace)
        all_facts = load_facts(conn, target_marketplace)
        path_rows = conn.execute(
            text(
                "SELECT source_id, direction, origin_marketplace, origin_import_id, origin_asin, origin_units, "
                "origin_sessions, origin_buybox_max_pct FROM acquisition_path "
                "WHERE stage IN ('ORIGIN_ASIN', 'VIA_SOURCE') AND origin_import_id = ANY(:ids) "
                "AND origin_units IS NOT NULL AND direction IS NOT NULL"
            ),
            {"ids": origin_import_ids},
        ).all()

    reps: dict[int, dict[str, RepOrigin]] = {}
    for p in path_rows:
        cand = RepOrigin(p.direction, p.origin_marketplace, p.origin_import_id, p.origin_asin,
                         int(p.origin_units), int(p.origin_sessions), p.origin_buybox_max_pct)
        current = reps.setdefault(p.source_id, {}).get(p.direction)
        if current is None or cand.sort_key() < current.sort_key():
            reps[p.source_id][p.direction] = cand

    blocked: list[tuple[SourceFacts, Decision]] = []
    needs_reaping: list[tuple[SourceFacts, Decision]] = []
    entries: dict[str, list[PlanEntry]] = {}
    for f in all_facts:
        d = decide(f, configs[f.source_type], now)
        if d.expired_run_ids:
            needs_reaping.append((f, d))
        if not d.eligible:
            blocked.append((f, d))
            continue
        group = {"ELIGIBLE_NEVER": "NEVER", "ELIGIBLE_CONTINUATION": "CONTINUE"}.get(d.state, "REFETCH")
        by_dir = reps.get(f.source_id, {})
        if group_by_direction:
            sections = sorted(by_dir) if by_dir else ["NO_ORIGIN"]
            for s in sections:
                entries.setdefault(s, []).append(PlanEntry(s, group, 0, f, d, by_dir.get(s), [x for x in sections if x != s]))
        else:
            best = min(by_dir.values(), key=lambda r: r.sort_key()) if by_dir else None
            s = "ALL" if best else "NO_ORIGIN"
            entries.setdefault(s, []).append(PlanEntry(s, group, 0, f, d, best))

    def order(e: PlanEntry):
        rep_key = e.rep.sort_key() if e.rep else (0, 0, 0, "")
        return ({"CONTINUE": 0, "NEVER": 1}.get(e.group, 2), 0 if e.rep else 1, rep_key if e.rep else (), e.facts.key_primary, e.facts.source_id)

    sections_out: dict[str, list[PlanEntry]] = {}
    for s in SECTION_ORDER:
        lst = sorted(entries.get(s, []), key=order)
        for i, e in enumerate(lst, start=1):
            e.rank = i
        if limit is not None:
            lst = lst[:limit]
        if lst:
            sections_out[s] = lst
    return Plan(target_marketplace, infos, now, sections_out, blocked, needs_reaping)
