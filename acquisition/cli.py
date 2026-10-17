"""acquisition CLI（段階2: 出典管理と再取得間隔）。実コレクタ・ブラウザ・統合ツールには接続しない。

実行例:
    python -m acquisition.cli register-source --market JP --type SELLER --key A29NE8W0SWJRBU \\
        --origin-import-id 1 --origin-asin B0076AZHC8
    python -m acquisition.cli acquire-plan --market JP --origin-import 1 --origin-import 2
    python -m acquisition.cli show-source --market JP --source-id 3
    python -m acquisition.cli legacy-dry-run
    python -m acquisition.cli legacy-apply --plan exports/acquisition/legacy_dry_run_*.csv --reason "..." --classify fact
    python -m acquisition.cli legacy-confirm --group <UUID> --market JP --reason "..."

--market は**取得先ページの市場**（その出典を取得するAmazonのサイト）。起点レポートの市場・業務方向
（輸出=US起点 / 輸入=JP起点）とは別の概念で、起点は --origin-import-id で明示する。

終了コード: 0=成功 / 1=失敗 / 2=拒否・指定不備。
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from acquisition.lease import AcquisitionRunService
from acquisition.legacy import LegacyError, LegacyService, read_plan_type_grades, read_selected_groups
from acquisition.legacy_scan import (
    DEFAULT_DONE_DIR,
    DEFAULT_SELLER_OUTPUT_ROOT,
    GRADE_LABEL,
    collect_collector_runs,
    collect_status_csv,
    collect_tool_runs,
    scan_done_dir,
    summarize,
    unclassified_review,
    write_dry_run_csv,
    write_unclassified_review,
)
from acquisition.normalize import SOURCE_TYPES
from acquisition.plan import Plan, PlanError, build_plan
from acquisition.registry import OriginError, SourceRegistry
from db.session import SessionFactory
from origin_selection.report_import import ReportImportService

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
DEFAULT_OUT_DIR = Path("exports") / "acquisition"
MARKETS = ["US", "JP"]


@dataclass
class Context:
    app_factory: SessionFactory
    registry: SourceRegistry
    runs: AcquisitionRunService
    legacy: LegacyService
    reports: ReportImportService


def _make_context() -> Context:
    f = SessionFactory.for_app_role()
    return Context(f, SourceRegistry(f), AcquisitionRunService(f), LegacyService(f), ReportImportService(f, None))


def _utc(ts: datetime | None) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


# ---------------------------------------------------------------------- 出典

def _cmd_register_source(a, ctx: Context) -> int:
    try:
        if a.origin_import_id is not None or a.origin_asin is not None:
            r = ctx.registry.register_path(a.market, a.type, a.key, a.qualifier, origin_import_id=a.origin_import_id,
                                           origin_asin=a.origin_asin, note=a.note)
            src, extra = r.source, f" 経路: {'追加' if r.path_created else '登録済み（変更なし）'}"
        else:
            r = ctx.registry.register_path(a.market, a.type, a.key, a.qualifier, note=a.note)
            src, extra = r.source, f" 経路(起点なし): {'追加' if r.path_created else '登録済み'}"
    except (OriginError, ValueError) as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(f"出典 source_id={src.source_id} {'新規' if src.created else '既存'} 取得先市場={src.marketplace} "
          f"種別={src.source_type} キー={src.key_primary!r}" + (f" 修飾={src.key_qualifier!r}" if src.key_qualifier else "") + extra)
    return EXIT_OK


def _decision_line(f, d) -> str:
    basis = {"RUN": "新方式の成功", "LEGACY": "旧証拠(参考)"}.get(d.gate_basis, "-")
    return (f"source_id={f.source_id} {f.source_type} {f.display_primary}"
            + (f" [{f.display_qualifier}]" if f.display_qualifier else "")
            + f" 状態={d.state} 最終成功={_utc(d.last_success_at)}"
            + (f"({d.last_success_outcome}{'/範囲限定' if d.last_success_completeness == 'TRUNCATED' else ''})" if d.last_success_at else "")
            + f" 旧証拠(確定)={_utc(d.legacy_confirmed_at)} 基準={basis} 再取得可能={_utc(d.due_at)}")


def _cmd_list_sources(a, ctx: Context) -> int:
    with ctx.app_factory.get_session(a.market) as conn, conn.begin():
        facts = load_facts(conn, a.market)
        cfgs = load_configs(conn, a.market)
        now = db_now(conn)
    n = 0
    for f in facts:
        d = decide(f, cfgs[f.source_type], now)
        if a.state and d.state != a.state:
            continue
        n += 1
        print(_decision_line(f, d))
    print(f"{n}件（取得先市場={a.market}）")
    return EXIT_OK


def _cmd_show_source(a, ctx: Context) -> int:
    with ctx.app_factory.get_session(a.market) as conn, conn.begin():
        facts = load_facts(conn, a.market, [a.source_id])
        if not facts:
            print(f"[中止] source_id={a.source_id} は市場{a.market}に存在しません")
            return EXIT_REFUSED
        f = facts[0]
        d = decide(f, load_configs(conn, a.market)[f.source_type], db_now(conn))
    print(_decision_line(f, d))
    print(f"  取得間隔={d.interval_days}日 連続失敗={d.consecutive_failures} 保留={f.status}/{f.hold_reason or '-'} {f.hold_detail or ''}")
    for b in d.blockers:
        print(f"  [{b.code}] {b.detail}")
    for s in f.legacy:
        print(f"  旧証拠: {s.evidence_kind} {s.evidence_ref} {_utc(s.evidence_at)} 状態={s.market_state}（{s.evidence_at_basis}）")
    if d.expired_run_ids:
        print(f"  期限切れの実行（回収待ち）: {d.expired_run_ids}")
    return EXIT_OK


# ---------------------------------------------------------------------- plan

def format_plan(plan: Plan) -> str:
    lines = [f"取得先市場={plan.target_marketplace} 判定時刻={_utc(plan.as_of)}",
             "使用レポート（明示指定）: " + ", ".join(
                 f"import_id={i.import_id} {i.marketplace} {i.period_start}〜{i.period_end}(利用者指定)" for i in plan.imports)]
    label = {"EXPORT": "輸出（US起点）", "IMPORT": "輸入（JP起点）", "ALL": "統合", "NO_ORIGIN": "起点なし"}
    for s, entries in plan.sections.items():
        lines.append(f"\n■ {label[s]}")
        for e in entries:
            rep = (f"代表起点 import={e.rep.import_id} {e.rep.asin} 注文{e.rep.units}点/セッション{e.rep.sessions}/"
                   f"獲得率{float(e.rep.buybox):g}%") if e.rep else "起点なし"
            shared = f" 共有取得(他: {','.join(e.also_in)})" if e.also_in else ""
            old = " ※旧証拠(参考)あり" if e.decision.gate_basis == "LEGACY" else ""
            lines.append(f"  {e.rank:>3}. [{'未取得' if e.group == 'NEVER' else '再取得'}] source_id={e.facts.source_id} "
                         f"{e.facts.source_type} {e.facts.display_primary} / {rep}{shared}{old}")
    lines.append(f"\n実行不可（順位なし）: {len(plan.blocked)}件")
    from collections import Counter
    for state, n in sorted(Counter(d.state for _, d in plan.blocked).items()):
        lines.append(f"  {state}: {n}件")
    if plan.needs_reaping:
        lines.append(f"期限切れの実行（reap-expiredで回収）: {len(plan.needs_reaping)}件")
    return "\n".join(lines)


PLAN_COLUMNS = ["section", "rank", "group", "source_id", "type", "key_primary", "key_qualifier", "display", "state",
                "rep_import_id", "rep_direction", "rep_asin", "rep_units", "rep_sessions", "rep_buybox_max_pct",
                "also_in", "last_success_at_utc", "last_success_outcome", "range_limited", "legacy_confirmed_at_utc",
                "gate_basis", "interval_days", "blockers"]


def write_plan_csv(plan: Plan, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(PLAN_COLUMNS)
        for s, entries in plan.sections.items():
            for e in entries:
                f, d, r = e.facts, e.decision, e.rep
                w.writerow([s, e.rank, e.group, f.source_id, f.source_type, f.key_primary, f.key_qualifier, f.display_primary,
                            d.state, r.import_id if r else "", r.direction if r else "", r.asin if r else "",
                            r.units if r else "", r.sessions if r else "", f"{float(r.buybox):g}" if r else "",
                            ",".join(e.also_in), _utc(d.last_success_at), d.last_success_outcome or "",
                            "Y" if d.last_success_completeness == "TRUNCATED" else "", _utc(d.legacy_confirmed_at),
                            d.gate_basis or "", d.interval_days, ""])
        for f, d in plan.blocked:
            w.writerow(["BLOCKED", "", "", f.source_id, f.source_type, f.key_primary, f.key_qualifier, f.display_primary,
                        d.state, "", "", "", "", "", "", "", _utc(d.last_success_at), d.last_success_outcome or "", "",
                        _utc(d.legacy_confirmed_at), d.gate_basis or "", d.interval_days,
                        " | ".join(f"{b.code}: {b.detail}" for b in d.blockers)])
    return path


def _cmd_acquire_plan(a, ctx: Context) -> int:
    try:
        plan = build_plan(ctx.app_factory, ctx.reports, a.market, a.origin_import, group_by_direction=not a.merged, limit=a.limit)
    except PlanError as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(format_plan(plan))
    n_uncl = ctx.legacy.unclassified_count(a.market)
    if n_uncl:
        print(f"\n[参考] 種別が未確定の旧履歴 {n_uncl}件（取得済みとも未取得とも確定していない。この計画には影響しない。"
              f"`legacy-list --unclassified` で確認し、人が分類するまで保留にも除外にも使わない）")
    if a.csv:
        print(f"\nCSV: {write_plan_csv(plan, Path(a.csv))}")
    return EXIT_OK


# ---------------------------------------------------------------------- 状態・回収

def _simple(call: Callable[[], None], ok_msg: str) -> int:
    try:
        call()
    except (ValueError, LegacyError) as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print(ok_msg)
    return EXIT_OK


def _cmd_hold(a, ctx): return _simple(lambda: ctx.registry.set_status(a.market, a.source_id, "ON_HOLD", a.reason), "保留にしました")
def _cmd_exclude(a, ctx): return _simple(lambda: ctx.registry.set_status(a.market, a.source_id, "EXCLUDED", a.reason), "除外にしました")
def _cmd_release(a, ctx): return _simple(lambda: ctx.registry.release(a.market, a.source_id, a.reason), "解除しました（以降、それ以前の失敗は連続失敗に数えません）")
def _cmd_set_interval(a, ctx): return _simple(
    lambda: ctx.registry.set_interval(a.market, a.source_id, None if a.clear else a.days, a.reason), "取得間隔を更新しました")
def _cmd_abort_run(a, ctx): return _simple(lambda: ctx.runs.abort_run(a.market, a.run_id, a.reason), "中断として確定しました（USER_ABORT。連続失敗には数えません）")


def _cmd_reap(a, ctx: Context) -> int:
    ids = ctx.runs.reap_expired(a.market)
    print(f"期限切れの実行を{len(ids)}件、INTERRUPTED(LEASE_EXPIRED)に確定しました: {ids}（旧経路の実行は対象外）")
    return EXIT_OK


# ---------------------------------------------------------------------- 旧履歴

def _scan(a, ctx: Context):
    obs = []
    stats = {}
    for m in MARKETS:
        obs += collect_tool_runs(ctx.app_factory, m)
        o, st = collect_collector_runs(ctx.app_factory, m)
        obs += o
        stats[m] = st
    root = Path(a.seller_output_root) if a.seller_output_root else DEFAULT_SELLER_OUTPUT_ROOT
    if root.exists():
        obs += collect_status_csv(root)
    return obs, stats


def _cmd_legacy_dry_run(a, ctx: Context) -> int:
    obs, stats = _scan(a, ctx)
    out = Path(a.out) if a.out else DEFAULT_OUT_DIR / f"legacy_dry_run_{datetime.now():%Y%m%d_%H%M%S}.csv"
    write_dry_run_csv(obs, out)
    print(f"旧履歴の観測 {len(obs)}件を{out}に書き出しました（DBは一切変更していません）")
    print("確認できた事実・推定・未確認の内訳（種別 / 成功 / 取得先市場 / 種別判定 / 30日以内か）:")
    for k, n in sorted(summarize(obs).items()):
        print(f"  {n:>5}件  " + " / ".join(k))
    for m, st in stats.items():
        print(f"  旧collector_run（市場{m}）: {st['total']}件、旧RUNNING/CAPTCHA_WAIT {st['legacy_running_untouched']}件（変更しません・回収対象にしません）")
    done = Path(a.done_dir) if a.done_dir else DEFAULT_DONE_DIR
    if done.exists():
        s = scan_done_dir(done)
        print(f"  done/（一覧のみ）: {s['files']}ファイル、実行日{len(s['days'])}日、キーワード延べ{s['keyword_rows']}行、"
              f"異なるもの{s['distinct']}、複数回現れるもの{s['repeated']} — {s['note']}")
    print("CSVの apply 列に Y を付けた行だけが legacy-apply の対象になります。")
    return EXIT_OK


def _cmd_legacy_apply(a, ctx: Context) -> int:
    selected = read_selected_groups(Path(a.plan))
    if not selected:
        print("[中止] apply列がYの行がありません")
        return EXIT_REFUSED
    obs, _ = _scan(a, ctx)
    if a.ledger_jp or a.ledger_us:  # legacy-select と同じ台帳指定で、種別の確認を再現する
        from acquisition.legacy_scan import apply_ledger_corroboration, enrich_heuristics, load_ledger_seller_ids

        enrich_heuristics(ctx.app_factory, obs)
        ids = {m: load_ledger_seller_ids(Path(path), sheet)
               for m, path, sheet in (("JP", a.ledger_jp, a.ledger_jp_sheet), ("US", a.ledger_us, a.ledger_us_sheet)) if path}
        print(f"台帳の根拠で種別を確認済みにした観測: {apply_ledger_corroboration(obs, ids)}件")
    if a.classify != "none":  # 計画作成時にFACTだった行を、同じ条件で再現できていなければ書き込み前に止める
        planned = read_plan_type_grades(Path(a.plan))
        now_grade = {o.group: o.type_grade for o in obs}
        lost = [g for g, grade in planned.items() if grade == "FACT" and now_grade.get(g) != "FACT"]
        if lost:
            print(f"[中止] 計画では種別が確認済み(FACT)の{len(lost)}件が、今回の読み取りでは確認できません。"
                  "legacy-select と同じ --ledger-jp/--ledger-us を指定してください（書き込みはしていません）")
            return EXIT_REFUSED
    try:
        stats = ctx.legacy.apply(obs, selected, classify=a.classify, confirm_recorded_market=a.confirm_recorded_market,
                                 reason=a.reason, batch_id=a.batch_id or f"{datetime.now():%Y%m%d%H%M%S}")
    except ValueError as exc:
        print(f"[中止] {exc}")
        return EXIT_REFUSED
    print("適用結果: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    missing = len(selected) - stats["selected"]
    if missing:
        print(f"[注意] 選択されたが現在の観測に存在しないグループ: {missing}件")
    return EXIT_OK


def _cmd_legacy_unclassified(a, ctx: Context) -> int:
    obs, _ = _scan(a, ctx)
    groups = unclassified_review(obs, recent_only=not a.all)
    out_dir = Path(a.out_dir) if a.out_dir else DEFAULT_OUT_DIR
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    s, d = write_unclassified_review(groups, out_dir / f"legacy_unclassified_summary_{stamp}.csv",
                                     out_dir / f"legacy_unclassified_detail_{stamp}.csv")
    print(f"種別が未分類の旧証拠（{'全期間' if a.all else '直近30日'}）: 異なる値{len(groups)}件、観測{sum(len(g) for g in groups)}件")
    print(f"  確認用の一覧（値ごと）: {s}")
    print(f"  明細（参照元・日時）: {d}")
    print("種別は推測していません。decided_type / decided_key 列は空欄です。分類は legacy-classify で人が決めます。")
    return EXIT_OK


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_intervals(ctx: Context) -> tuple[datetime, dict[tuple[str, str], int], list[str]]:
    """DB時刻と、取得先市場×種別の取得間隔(日)を読む（読み取り専用）。"""
    from sqlalchemy import text

    notes: list[str] = []
    table: dict[tuple[str, str], int] = {}
    as_of = None
    for m in MARKETS:
        with ctx.app_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            if as_of is None:
                as_of = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            try:
                for r in conn.execute(text("SELECT source_type, min_interval_days FROM acquisition_interval_config")):
                    table[(m, r.source_type)] = r.min_interval_days
            except Exception as exc:  # 0016未適用のDBなど
                notes.append(f"取得間隔の設定を読めないため既定30日を使います: {type(exc).__name__}")
    return as_of, table, notes


def _cmd_legacy_select(a, ctx: Context) -> int:
    from acquisition.legacy_scan import (
        EXCLUSION_ORDER, REASON_LABEL, apply_ledger_corroboration, enrich_heuristics, load_ledger_seller_ids,
        select_for_migration, summarize_selection, write_heuristic_csv, write_selection_csv,
    )

    as_of, table, notes = _load_intervals(ctx)

    def interval_days(source_type, market):
        markets = [market] if market else MARKETS
        types = [source_type] if source_type else list(SOURCE_TYPES)
        vals = [table.get((m, t), 30) for m in markets for t in types]
        return max(vals)

    obs, stats = _scan(a, ctx)
    enrich_heuristics(ctx.app_factory, obs)
    ledger_ids = {}
    for m, path, sheet in (("JP", a.ledger_jp, a.ledger_jp_sheet), ("US", a.ledger_us, a.ledger_us_sheet)):
        if path:
            ledger_ids[m] = load_ledger_seller_ids(Path(path), sheet)
    promoted = apply_ledger_corroboration(obs, ledger_ids) if ledger_ids else 0
    rows = select_for_migration(obs, interval_days, as_of)
    out_dir = Path(a.out_dir) if a.out_dir else DEFAULT_OUT_DIR
    stamp = f"{as_of.astimezone(timezone.utc):%Y%m%d_%H%M%S}UTC"
    dry = write_dry_run_csv(obs, out_dir / f"legacy_dry_run_{stamp}.csv", now=as_of)  # 元のdry-run（apply列は全行未選択）
    sel = write_selection_csv(rows, out_dir / f"legacy_selection_{stamp}.csv", as_of)  # 選択結果（別ファイル）
    obs_only = write_selection_csv(rows, out_dir / f"legacy_observe_only_{stamp}.csv", as_of, observe_only=True)
    heur, hs = write_heuristic_csv(rows, out_dir / f"legacy_heuristic_candidates_{stamp}.csv")
    groups = unclassified_review(obs, recent_only=True, now=as_of)
    us, ud = write_unclassified_review(groups, out_dir / f"legacy_unclassified_summary_{stamp}.csv",
                                       out_dir / f"legacy_unclassified_detail_{stamp}.csv", now=as_of)
    s = summarize_selection(rows)

    lines = [
        f"基準時刻(DB時刻): {as_of.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
        "選択条件（すべて満たすもの）: (1)種別・出典キーを確認できる(記録/URL由来。ID形式の推定は含めない) "
        "(2)証拠時刻が取得間隔内 (3)取得成功の承認済み参考証拠(推定)がある",
        "取得間隔(日): " + ", ".join(f"{m}/{t}={d}" for (m, t), d in sorted(table.items())) if table else "取得間隔: 既定30日",
        f"観測の合計: {s['observations_total']}件 → 選択: {s['selected_observations']}件 / 除外: {s['excluded_total']}件",
        "参照元別の選択件数（証拠数）: " + (", ".join(f"{k}={v}" for k, v in sorted(s['selected_by_kind'].items())) or "なし"),
        "扱い別: " + (", ".join(f"{k}={v}" for k, v in sorted(s['selected_by_decision'].items())) or "なし")
        + "（SELECT_BOTH=取得先市場不明→US・JP双方のREVIEW_LEGACY用 / SELECT_RECORDED_MARKET=記録で市場が分かる参考証拠）",
        f"対象出典数: {s['sources_total']}（うち、両市場で保留になる出典={s['sources_held_in_both_markets']}、"
        f"記録の市場で参考証拠として保持=US {s['sources_confirmed_by_market']['US']} / JP {s['sources_confirmed_by_market']['JP']}）",
        f"予定のDB行数: {s['planned_rows']}行（" + ", ".join(f"{k}={v}" for k, v in s['planned_rows_breakdown'].items()) + "）",
        "除外理由別件数（順に最初に該当した1つ）:",
    ]
    for code, label in EXCLUSION_ORDER:
        by_kind = s["excluded_by_reason_and_kind"][code]
        lines.append(f"  {s['excluded_by_reason'][code]:>5}件  {label}  " + (str(by_kind) if by_kind else ""))
    if ledger_ids:
        lines.append("セラー台帳の根拠で種別を確認済みにした観測: %d件（台帳ID数 %s）" % (promoted, ", ".join(f"{m}={len(v)}" for m, v in sorted(ledger_ids.items()))))
    lines.append(f"誤分類候補（ID形式の推定のみ・選択しない）: 観測{hs['observations']}件、異なる値{hs['distinct_values']}件")
    for lab, n in sorted(hs["by_label"].items()):
        lines.append(f"    {n:>4}件（異なる値{hs['distinct_by_label'][lab]}） {lab}")
    lines.append(f"未分類（直近の取得間隔内）: 異なる値{len(groups)}件、観測{sum(len(g) for g in groups)}件 — 推測しない")
    for m, st in stats.items():
        lines.append(f"旧collector_run(市場{m}): {st['total']}件、うち旧RUNNING/CAPTCHA_WAIT {st['legacy_running_untouched']}件（選択しない）")
    done = Path(a.done_dir) if a.done_dir else DEFAULT_DONE_DIR
    if done.exists():
        d = scan_done_dir(done)
        lines.append(f"done/: {d['files']}ファイル（一覧のみ・選択しない）")
    lines += notes
    files = {"dry-run(元・apply列は全行未選択)": dry, "選択結果": sel, "観測のみ(種別未確認・分類しない)": obs_only, "誤分類候補": heur, "未分類(要約)": us, "未分類(明細)": ud}
    lines.append("ファイルとsha256:")
    for label, p in files.items():
        lines.append(f"  {label}: {p}  sha256={_sha256(p)}")
    summary = out_dir / f"legacy_selection_summary_{stamp}.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"要約: {summary}\nDBは一切変更していません。適用は legacy-apply --plan <選択結果CSV> --classify fact --confirm-recorded-market（別途指示）。")
    return EXIT_OK


def _cmd_own_account_add(a, ctx: Context) -> int:
    from sqlalchemy import text

    with ctx.app_factory.get_session(a.market) as conn, conn.begin():
        conn.execute(
            text("INSERT INTO marketplace_account_config (marketplace, account_id, is_active) VALUES (:m, :a, true) "
                 "ON CONFLICT (marketplace, account_id) DO UPDATE SET is_active = true"),
            {"m": a.market, "a": a.account_id.strip().upper()},
        )
    print(f"自社セラーID {a.account_id.strip().upper()} を市場{a.market}に設定しました（商品ページからの発見で除外されます）")
    return EXIT_OK


def _cmd_own_account_list(a, ctx: Context) -> int:
    from sqlalchemy import text

    for m in MARKETS:
        with ctx.app_factory.get_session(m) as conn, conn.begin():
            rows = conn.execute(text("SELECT account_id, is_active FROM marketplace_account_config ORDER BY account_id")).all()
        print(f"{m}: " + (", ".join(f"{r.account_id}{'' if r.is_active else '(無効)'}" for r in rows) or "未設定"))
    return EXIT_OK


def _cmd_legacy_list(a, ctx: Context) -> int:
    n = 0
    for g in ctx.legacy.list_groups():
        s = g["state"]
        if a.unclassified and s.classified:
            continue
        if a.state and a.state not in g["partitions"].values():
            continue
        n += 1
        part = ",".join(f"{p}:{st}" for p, st in sorted(g["partitions"].items()))
        print(f"{g['group']} {s.evidence_kind} {s.evidence_ref} 値={s.raw_value!r} 証拠時刻={_utc(s.evidence_at)} "
              f"成功={GRADE_LABEL[s.success_grade]} 種別={s.source_type or '未分類'} 状態[{part}]")
    print(f"{n}件")
    return EXIT_OK


def _cmd_legacy_classify(a, ctx): return _simple(lambda: print("分類した区画:", ctx.legacy.classify(a.group, a.type, a.key, a.qualifier, a.reason)), "分類を記録しました")
def _cmd_legacy_confirm(a, ctx): return _simple(lambda: print("確定を記録した区画:", ctx.legacy.confirm(a.group, a.market, a.reason)), "確定しました（取得成功の断定ではなく、旧証拠として扱います）")
def _cmd_legacy_dismiss(a, ctx): return _simple(lambda: print("却下した区画:", ctx.legacy.dismiss(a.group, a.reason)), "却下を記録しました")
def _cmd_legacy_reopen(a, ctx): return _simple(lambda: print("再開した区画:", ctx.legacy.reopen(a.group, a.reason)), "未確定に戻しました")


# ---------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m acquisition.cli")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_, market=True):
        sp = sub.add_parser(name, help=help_)
        if market:
            sp.add_argument("--market", required=True, choices=MARKETS, help="取得先ページの市場（起点レポートの市場ではない）")
        sp.set_defaults(func=func)
        return sp

    sp = add("register-source", _cmd_register_source, "出典と発見経路を登録する（同じ出典は1つに集約し、経路だけ追加）")
    sp.add_argument("--type", required=True, choices=list(SOURCE_TYPES))
    sp.add_argument("--key", required=True)
    sp.add_argument("--qualifier", default="", help="KEYWORDのカテゴリ別名など")
    sp.add_argument("--origin-import-id", type=int, default=None)
    sp.add_argument("--origin-asin", default=None)
    sp.add_argument("--note", default=None)

    sp = add("list-sources", _cmd_list_sources, "出典の一覧と現在の判定")
    sp.add_argument("--state", default=None)
    sp = add("show-source", _cmd_show_source, "出典の詳細（判定の理由・旧証拠・期限切れ実行）")
    sp.add_argument("--source-id", required=True, type=int)

    sp = add("acquire-plan", _cmd_acquire_plan, "取得計画（判定→順位付け。使うレポートは明示指定）")
    sp.add_argument("--origin-import", required=True, type=int, action="append", help="使うレポートのimport_id（市場ごとに1つ。複数指定可）")
    sp.add_argument("--merged", action="store_true", help="方向別ではなく統合して順位付けする")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--csv", default=None)

    for name, func, help_ in (("hold-source", _cmd_hold, "出典を保留にする"), ("exclude-source", _cmd_exclude, "出典を除外する"),
                              ("release-hold", _cmd_release, "保留・除外・自動保留を明示的に解除する")):
        sp = add(name, func, help_)
        sp.add_argument("--source-id", required=True, type=int)
        sp.add_argument("--reason", required=True)
    sp = add("set-interval", _cmd_set_interval, "出典ごとの取得間隔(日)を上書きする")
    sp.add_argument("--source-id", required=True, type=int)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--days", type=int)
    g.add_argument("--clear", action="store_true")
    sp.add_argument("--reason", required=True)
    sp = add("abort-run", _cmd_abort_run, "新方式の実行を人が中断として確定する（旧経路の行は対象外）")
    sp.add_argument("--run-id", required=True, type=int)
    sp.add_argument("--reason", required=True)
    add("reap-expired", _cmd_reap, "期限切れ（DB時刻）の新方式の実行を回収する")

    sp = add("legacy-dry-run", _cmd_legacy_dry_run, "旧履歴を読み取り、事実/推定/未確認を分けてCSVに出す（DBは変更しない）", market=False)
    sp.add_argument("--out", default=None)
    sp.add_argument("--seller-output-root", default=None)
    sp.add_argument("--done-dir", default=None)
    sp = add("legacy-select", _cmd_legacy_select, "最新のDB時刻でdry-runを再生成し、移行対象案（選択結果CSV）と誤分類候補・未分類の一覧を作る（読み取り専用）", market=False)
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--seller-output-root", default=None)
    sp.add_argument("--done-dir", default=None)
    sp.add_argument("--ledger-jp", default=None, help="JPセラー台帳(xlsx)。seller id列でID形式の推定を確認済みにする")
    sp.add_argument("--ledger-jp-sheet", default="セラーリスト")
    sp.add_argument("--ledger-us", default=None, help="USセラー台帳(xlsx)")
    sp.add_argument("--ledger-us-sheet", default="セラー台帳")
    sp = add("legacy-unclassified", _cmd_legacy_unclassified, "種別が自動で決まらなかった旧証拠の確認用一覧を出力する（推測しない・読み取り専用）", market=False)
    sp.add_argument("--all", action="store_true", help="直近30日に限らず全期間")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--seller-output-root", default=None)
    sp = add("legacy-apply", _cmd_legacy_apply, "dry-run CSVでapply=Yにした行だけを適用する", market=False)
    sp.add_argument("--plan", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--classify", choices=["none", "fact", "all"], default="none", help="種別の自動提案を採用する範囲（既定: 採用しない）")
    sp.add_argument("--confirm-recorded-market", action="store_true", help="記録(URLドメイン)で市場が事実の行を、その市場で確定する")
    sp.add_argument("--batch-id", default=None)
    sp.add_argument("--seller-output-root", default=None)
    sp.add_argument("--ledger-jp", default=None, help="legacy-select と同じJP台帳(xlsx)")
    sp.add_argument("--ledger-jp-sheet", default="セラーリスト")
    sp.add_argument("--ledger-us", default=None, help="legacy-select と同じUS台帳(xlsx)")
    sp.add_argument("--ledger-us-sheet", default="セラー台帳")
    sp = add("own-account-add", _cmd_own_account_add, "自社セラーIDを設定する（商品ページからの発見で除外する）", market=True)
    sp.add_argument("--account-id", required=True)
    add("own-account-list", _cmd_own_account_list, "自社セラーIDの設定状況", market=False)
    sp = add("legacy-list", _cmd_legacy_list, "適用済みの旧証拠と状態", market=False)
    sp.add_argument("--unclassified", action="store_true")
    sp.add_argument("--state", choices=["UNKNOWN", "CONFIRMED_THIS", "CONFIRMED_OTHER", "DISMISSED"], default=None)
    sp = add("legacy-classify", _cmd_legacy_classify, "未分類の旧証拠の種別・キーを人が決める", market=False)
    sp.add_argument("--group", required=True)
    sp.add_argument("--type", required=True, choices=list(SOURCE_TYPES))
    sp.add_argument("--key", required=True)
    sp.add_argument("--qualifier", default="")
    sp.add_argument("--reason", required=True)
    sp = add("legacy-confirm", _cmd_legacy_confirm, "旧証拠の取得先市場を確定する（両区画を整合して更新）", market=True)
    sp.add_argument("--group", required=True)
    sp.add_argument("--reason", required=True)
    for name, func, help_ in (("legacy-dismiss", _cmd_legacy_dismiss, "取得の証拠ではないと判断して無視する"),
                              ("legacy-reopen", _cmd_legacy_reopen, "確定・却下を取り消して未確定に戻す（履歴は残る）")):
        sp = add(name, func, help_, market=False)
        sp.add_argument("--group", required=True)
        sp.add_argument("--reason", required=True)
    return p


def main(argv: list[str] | None = None, ctx_factory: Callable[[], Context] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = (ctx_factory or _make_context)()
    return args.func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
