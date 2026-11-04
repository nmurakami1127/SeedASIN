"""ひとつの実行入口。

    python -m workflow.cli init-config [--out config/workflow.json]      # 例（config/workflow.example.json）をコピー。import_idを書き換える
    python -m workflow.cli plan   --config config/workflow.json           # 外部アクセス・DB更新なし。対象・予定・上限を表示
    python -m workflow.cli run    --config config/workflow.json           # 計画に従って実行（人の操作が必要な所で待機/保留）
    python -m workflow.cli status [--id wf-…]                             # 工程別の完了・待機・保留・失敗・結果不明
    python -m workflow.cli resume [--id wf-…] [--allow-duplicate <投入キー>]   # 保存済み状態から再開（完了工程は繰り返さない）
    python -m workflow.cli outcome --market US|JP|ALL [--since …] [--until …]  # 成果（読み取りのみ。取得・初見ASIN・投入・持ち越し）
    python -m workflow.cli reconcile [--apply]                             # 未完了workflowの整理。既定はdry-run（何も書かない）
    python -m workflow.cli backup  --out backups/wf.zip                    # workflow状態・投入台帳・マニフェストをまとめて保存
    python -m workflow.cli restore --zip backups/wf.zip [--force]          # 検証して復元

実行のたびにセレクタ・ログJSON・options JSON・内部IDを入力しない。設定ファイルはGit管理しない（import_id等の運用値）。
終了コード: 0=完了 / 2=設定・実行不可 / 3=人の操作が必要（待機・保留・結果不明）または失敗あり（resumeで再開）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from workflow.backup import BackupError, create_backup, restore_backup
from workflow.config import ConfigError, config_from_dict, load_config
from workflow.engine import KIND_LABEL, Engine
from workflow.store import DONE_STATES, State, StateCorrupt, WorkflowBusy, WorkflowStore, usage

EXAMPLE = Path("config") / "workflow.example.json"
JA = {"PENDING": "未着手", "DONE": "完了", "SKIPPED": "省略", "WAITING": "待機（人の操作）", "HELD": "保留", "FAILED": "失敗（resumeで再試行）",
      "UNKNOWN": "結果不明（再送せず照合）"}


def overall(st: State) -> str:
    s = {t.status for t in st.tasks.values()}
    if s <= set(DONE_STATES):
        return "COMPLETED"
    if "FAILED" in s:
        return "FAILED_RETRYABLE"
    if s & {"WAITING", "HELD", "UNKNOWN"}:
        return "NEEDS_ACTION"
    return "IN_PROGRESS"


PLAN_LABEL = {"CONTINUE": "(続き)", "FINALIZE": "(末尾到達の確定・取得なし)", "RETRY_FIRST": "(1ページ目から再試行)",
              "RESTART_BROKEN": "(取り直し:位置を信頼できない)", "RESTART_EXPIRED": "(取り直し:放置期限切れ)"}


def mode_summary(t) -> str:
    """工程ごとの 再利用／新規実行／スキップ／待機 の内訳。"""
    c: dict[str, int] = {}

    def add(k):
        c[k] = c.get(k, 0) + 1

    if t.kind == "discover":
        for i in t.result.get("items", []):
            add({"REUSED": "再利用(保存済みの調査結果)", "FOUND": "新規取得(商品ページ)", "NOT_FOUND": "新規取得(該当なし)"}.get(i["kind"], i["kind"]))
    elif t.kind == "cross":
        for v in t.result.get("via", {}).values():
            for k in v.get("kinds", []):
                add({"REUSED": "再利用(保存済みの調査結果)", "FOUND": "新規取得(商品ページ)", "NOT_FOUND": "新規取得(該当なし)"}.get(k, k))
            if v.get("note"):
                add("スキップ(上限)")
    elif t.kind in ("acquire", "acquire_x"):
        for r in t.result.get("sources", {}).values():
            lab = {"REUSED": "再利用(保存済みの取得結果)", "FETCHED": "新規取得", "EMPTY": "新規取得(正常な0件)", "HELD": "保留",
                   "CAPTCHA": "待機(CAPTCHA)", "FAILED": "失敗", "SKIPPED_AT_CLAIM": "スキップ",
                   "SKIPPED_POLICY": "枝をスキップ(除外/保留/期間内)"}.get(r["mode"], r["mode"])
            if r["mode"] == "HELD" and r.get("pause") == "PAUSED_PAGE_CAP":
                lab = "正常な途中終了(取得ページの上限・次回に持ち越し)"
            elif r["mode"] in ("FETCHED", "EMPTY"):
                lab += PLAN_LABEL.get(r.get("plan"), "")
                lab += "・一巡完了" if r.get("cycle_complete") else "・上限打切り" if r.get("truncated") else ""  # 打切りと取得完了を区別する
            elif r["mode"] == "HELD" and r.get("daily"):
                lab += "(日次上限・翌日に続きから)" if r.get("outcome") == "DAILY_LIMIT" else "(日次上限)"
            elif r["mode"] == "SKIPPED_POLICY" and r.get("needs_release"):
                lab += "・要解除(release)"
            elif r.get("truncated"):
                lab += "・上限打切り"
            add(lab)
    elif t.kind == "submit":
        add("引き継ぎ(既存の対応記録)" if t.result.get("adopted") else "新規投入" if t.result.get("send_started") else "未送信")
    elif t.kind == "expand":
        for _ in t.result.get("keys", []):
            add("投入計画")
    return "〔" + "・".join(f"{k}{v}" for k, v in c.items()) + "〕" if c else ""


def status_lines(st: State) -> list[str]:
    lines = [f"workflow {st.workflow_id}  作成 {st.created_at}  設定sha256={st.config_sha256[:12]}…  全体: {overall(st)}"]
    by: dict[str, dict[str, int]] = {}
    for t in st.tasks.values():
        by.setdefault(t.kind, {}).setdefault(t.status, 0)
        by[t.kind][t.status] += 1
    for kind, label in KIND_LABEL.items():
        if kind in by:
            lines.append(f"  {label:<8} " + " ".join(f"{JA.get(k, k)}:{v}" for k, v in sorted(by[kind].items())))
    actions = []
    for t in st.tasks.values():
        mark = JA.get(t.status, t.status)
        lines.append(f"    - {t.id} [{t.lane}] {mark}{mode_summary(t)}" + (f": {t.detail}" if t.detail else ""))
        if t.status in ("WAITING", "HELD", "UNKNOWN", "FAILED"):
            actions.append(f"{t.id}（{t.lane}）: {t.detail}")
    held = [(t.lane, h) for t in st.tasks.values() if t.kind == "discover" for h in t.result.get("held", [])]
    for lane, h in held:
        lines.append(f"    ! [{lane}] Amazon小売かの判定待ち: {h['key']}（{(h.get('detail') or '')[:80]}）— 人が判断。登録していません")
    u = usage(st)
    lines.append(f"  この実行で新しく使った量: Amazon新規取得 {u['pages']}ページ(出典{u['source_pages']}+商品ページ{u['product_pages']}) / 新規出典 {u['new_sources']}件 / "
                 f"新規投入 {u['new_submissions']}件（引き継ぎ {u['adopted_submissions']}件）")
    handed = [t for t in st.tasks.values() if t.kind == "submit" and t.status == "DONE" and t.result.get("handoff")]
    if handed:
        lines.append(f"  投入台帳へ引き渡し(HANDED_OFF): {len(handed)}件 ← 登録完了ではありません。未登録のASINは投入台帳で管理されます")
    carried = [t for t in st.tasks.values() if t.result.get("closed_reason") == "CARRIED_OVER"]
    for t in carried:
        lines.append(f"  持ち越し: {t.id} の{len(t.result.get('carried_over', []))}出典を出典単位の再開待ちとして引き継ぎ済み（{t.result.get('handed_off_to')}）")
    done_sub = [t for t in st.tasks.values() if t.kind == "submit" and t.status == "DONE" and not t.result.get("handoff")]
    if done_sub:
        lines.append("  対応記録済みの投入:")
        for t in done_sub:
            r = t.result
            lines.append(f"    - run{r.get('run_id')} {r.get('direction')} {r.get('memo')} ASIN{r.get('asin_count')} → 実行ID={r.get('tool_run_id')}"
                         + ("（保存済み結果の再利用）" if r.get("reused") else ""))
    if actions:
        lines.append("  再開方法: 上の対応（ログイン/CAPTCHA/確認）を済ませてから `python -m workflow.cli resume`。完了済みの工程は繰り返しません。")
    lines.append("  統合ツールの処理完了（待機中・取得中）は完了条件に含みません。")
    return lines


def _factory():
    from db.session import SessionFactory

    return SessionFactory.for_app_role()


def _readonly_factory():
    """読み取り専用コマンド用: 環境変数が無ければローカル開発DBのapp_roleに接続する（workflow/dbconn.py）。"""
    from workflow.dbconn import readonly_factory, resolve_app_url

    url, origin = resolve_app_url()
    print(f"[接続] app_role（読み取り専用）: {origin}")
    return readonly_factory()


def _engine(cfg, factory, ports):
    from workflow.ports import RealPorts

    return Engine(factory or _factory(), ports or RealPorts(), cfg, WorkflowStore(cfg.state_dir))


def _latest(store: WorkflowStore, wid: str | None) -> str:
    if wid:
        return wid
    ids = store.list_ids()
    if not ids:
        raise SystemExit("[中止] workflowがありません（先に run）")
    return ids[-1]


def stall_status_lines(st: dict | None, jst=lambda x: x or "-") -> list[str]:
    """止まりの検知（workflow/stall.py）の表示。状態ファイルに無い（古いwatch・未確認）なら何も出さない。"""
    if not st:
        return []
    lines = [f"  停止の兆候の確認（最終 {jst(st.get('checked_at'))}）"]
    if st.get("error"):
        lines.append(f"    ! 確認に失敗: {st['error']}")
    for m, mk in sorted((st.get("markets") or {}).items()):
        if mk.get("error"):
            lines.append(f"    [{m}] 確認に失敗: {mk['error']}")
            continue
        na = mk.get("new_asin") or {}
        if na.get("status") == "UNKNOWN":
            new = f"初見ASIN 直近24時間{na.get('last24')}件（判定不能: {na.get('reason') or '7日分の日次データが足りません'}）"
        elif na.get("median7d") is None or not na.get("median7d"):
            new = f"初見ASIN 直近24時間{na.get('last24')}件（直近7日の日次の中央値が0のため比較なし）"
        else:
            new = (f"初見ASIN 直近24時間{na.get('last24')}件／直近7日の日次中央値{na.get('median7d'):g}件"
                   f"（{na.get('percent_of_median'):.0f}%・{na.get('threshold_percent')}%未満で警告）")
        lines.append(f"    [{m}] 要解除{mk.get('needs_release_count', 0)}件 ・ 商品ページSERVER_ERROR連続{mk.get('server_error_streak', 0)}回 ・ {new}")
        for it in (mk.get("needs_release") or [])[:10]:
            lines.append(f"        要解除 #{it['source_id']} {it['source_type']} {it['name']}（{'/'.join(it['codes'])}"
                         f"{'・' + it['hold_reason'] if it.get('hold_reason') else ''}）")
        extra = mk.get("needs_release_count", 0) - min(10, len(mk.get("needs_release") or []))
        if extra > 0:
            lines.append(f"        ほか{extra}件（python -m acquisition.cli で確認）")
        for w in mk.get("warnings") or []:
            lines.append(f"    ! {w}")
    return lines


def watch_status_lines(d: dict | None) -> list[str]:
    if not d:
        return ["常駐(watch): 未起動（または状態ファイルなし）"]
    from workflow.daily_usage import JST
    from datetime import datetime

    def jst(x):
        return datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=__import__("datetime").timezone.utc).astimezone(JST).strftime("%m-%d %H:%M JST") if x else "-"

    label = {"running": "稼働中", "idle": "待機", "waiting_human": "人手対応待ち", "stopped": "停止"}.get(d["state"], d["state"])
    if not d.get("alive") and d["state"] != "stopped":
        label += "（プロセスは終了済み）"
    lines = [f"常駐(watch): {label} / 次回確認 {jst(d.get('next_check'))} / 最終更新 {jst(d.get('updated_at'))} / 起動 {jst(d.get('started_at'))}（確認{d.get('cycles', 0)}回）",
             f"  理由・市場別: {d.get('reason') or '-'}"]
    for k, v in (d.get("lanes") or {}).items():
        lines.append(f"    [{k}] {v}")
    for h in d.get("human") or []:
        lines.append(f"    ! 人手対応待ち: {h}（対応後に次回確認で自動再開。すぐ再開するなら `workflow.cli resume`※watch停止中のみ）")
    lines.extend(stall_status_lines(d.get("stall"), jst))
    u, s_, c = d.get("usage_today", {}), d.get("usage_session", {}), d.get("caps", {})
    lines.append(f"  本日(JST {d.get('today_jst')})の使用量: ページ{u.get('pages', 0)}/{c.get('pages') or '無制限'} ・ 輸出{u.get('submit:EXPORT', 0)}・輸入{u.get('submit:IMPORT', 0)}"
                 f"/{c.get('submit_per_direction') or '無制限'}件 ・ ASIN{u.get('asins', 0)}/{c.get('asins') or '無制限'}")
    lines.append(f"  今回の起動以降: ページ{s_.get('pages', 0)} ・ 輸出{s_.get('submit:EXPORT', 0)}・輸入{s_.get('submit:IMPORT', 0)} ・ ASIN{s_.get('asins', 0)}")
    return lines


def _print_status(st: State, state_dir: str | None = None) -> int:
    print("\n".join(status_lines(st)))
    o = overall(st)
    return 0 if o == "COMPLETED" else 3


def _cmd_init(a, **_) -> int:
    out = Path(a.out)
    if out.exists():
        print(f"[中止] 既に存在します（上書きしません）: {out}")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE, out)
    print(f"設定の例をコピーしました: {out}\n  lanes の origin_import を、市場ごとのACTIVEレポートのimport_idに書き換えてください。")
    return 0


def _cmd_plan(a, factory=None, ports=None) -> int:
    from workflow.planning import build_plan_text

    cfg = load_config(a.config)
    lines, blockers = build_plan_text(_engine(cfg, factory, ports))
    print("\n".join(lines))
    if blockers:
        print("\n[実行できません] " + " / ".join(blockers))
        return 2
    return 0


def _cmd_run(a, factory=None, ports=None) -> int:
    cfg = load_config(a.config)
    eng = _engine(cfg, factory, ports)
    with eng.store.engine_lock("run"):  # watch・他のrun/resumeと同時に外部を操作しない
        st = eng.new_state()
        print(f"workflow {st.workflow_id} を開始します（設定を保存済み。以後の再開はこの設定で行います）")
        with eng.store.lock(st.workflow_id):
            eng.drive()
    return _print_status(eng.st)


def _cmd_resume(a, factory=None, ports=None) -> int:
    store = WorkflowStore(a.state_dir)
    wid = _latest(store, a.id)
    st = store.load(wid)
    cfg = config_from_dict(st.config)  # 市場・起点importは、起動時に保存した設定スナップショットで再開する
    if a.config and Path(a.config).exists():  # 上限・間隔などは現在の設定を使う（上限の引き上げを反映できる）
        cur = load_config(a.config)
        cur.lanes = dict(cfg.lanes)
        cfg = cur
    eng = _engine(cfg, factory, ports)
    eng.store, eng.st = store, st
    for k in a.allow_duplicate or []:
        if k not in st.allow_duplicates:
            st.allow_duplicates.append(k)
    with store.engine_lock("resume"):
        with store.lock(wid):
            eng.prepare_resume()
            eng.drive()
    return _print_status(eng.st)


def _cmd_watch(a, factory=None, ports=None) -> int:
    from workflow.ports import RealPorts
    from workflow.watch import Watcher

    cfg = load_config(a.config)
    return Watcher(cfg, factory or _factory(), ports or RealPorts()).run(max_cycles=a.max_cycles)


def _cmd_fetch_source(a, factory=None, ports=None) -> int:
    """対象の出典を1回だけ取得する（日次のページ使用量とengine_lockを通す。配送先・自社除外・保留・旧証拠・leaseは通常の取得と同じ）。"""
    cfg = load_config(a.config)
    eng = _engine(cfg, factory, ports)
    with eng.store.engine_lock("fetch-source"):
        out = eng.fetch_source(a.market, a.source_id, a.max_pages, total_cap=a.total_cap)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0 if out.get("status") == "RAN" else 3


def _cmd_stop(a, **_) -> int:
    """稼働中のwatchに、安全な区切り（実行中の工程が終わった後）で停止するよう依頼する（Ctrl+Cが使えない時用）。"""
    store = WorkflowStore(a.state_dir)
    info = store.engine_lock_info()
    if not info:
        print("稼働中の watch / run / resume はありません（停止要求は作りません）")
        return 0
    Path(a.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(a.state_dir) / "stop.request").write_text("stop", encoding="utf-8")
    print(f"停止を依頼しました（pid={info.get('pid')}・{info.get('who')}）。実行中の工程が終わり次第、状態を保存して止まります")
    return 0


def _cmd_yield(a, factory=None, **_) -> int:
    from workflow.yield_report import build_yield, format_yield

    cfg = load_config(a.config) if a.config and Path(a.config).exists() else None
    print("\n".join(format_yield(build_yield(factory or _factory(), cfg.submission_dir if cfg else a.submission_dir))))
    return 0


def _cmd_status(a, **_) -> int:
    from workflow.daily_usage import DailyUsage
    from workflow.planning import usage_lines
    from workflow.watch import read_status

    store = WorkflowStore(a.state_dir)
    print("\n".join(watch_status_lines(read_status(a.state_dir))))
    ids = store.list_ids()
    if not ids and not a.id:
        print("workflowはまだありません（run または watch）")
        return 0
    st = store.load(_latest(store, a.id))
    rc = _print_status(st)
    print("\n".join(usage_lines(config_from_dict(st.config), DailyUsage(Path(a.state_dir) / "daily_usage.jsonl"))))
    unfinished = [w for w in ids if overall(store.load(w)) != "COMPLETED"]
    if unfinished:
        print(f"  未完了のworkflow: {unfinished}（watchが再開します。手動なら resume --id）")
    return rc


def _cmd_outcome(a, factory=None, **_) -> int:
    from workflow.outcome import build_outcome, format_outcome, parse_when
    from datetime import datetime, timedelta, timezone

    markets = ["US", "JP"] if a.market == "ALL" else [a.market]
    now = datetime.now(timezone.utc)
    until = parse_when(a.until, now)
    since = parse_when(a.since, until - timedelta(hours=24))
    if not since < until:
        print("[中止] --since は --until より前にしてください")
        return 2
    fac = factory or _readonly_factory()
    selectable, needed, pps = None, 4, 60
    if a.config:   # 設定があれば、選択可能な出典数（後退中でない）を計画から数える（読み取りのみ）
        selectable, cfg = {}, load_config(a.config)
        needed, pps = cfg.needed_sources(), cfg.limits.pages_per_source
        for m in markets:
            selectable[m] = _selectable(cfg, fac, m)
    o = build_outcome(fac, markets, since, until, state_dir=a.state_dir, submission_dir=a.submission_dir, top=a.top,
                      selectable=selectable, needed=needed, pages_per_source=pps)
    print("\n".join(format_outcome(o)))
    return 0


def _selectable(cfg, fac, market: str):
    """この市場の取得先で、選択可能（後退中でない）な出典数。計画を作れなければNone。"""
    from acquisition.plan import PlanError, build_plan
    from origin_selection.report_import import ReportImportService
    from workflow.progress import SourceProgress
    from workflow.source_select import choose

    reports = ReportImportService(fac, None, actor="workflow-outcome")
    prog = SourceProgress(Path(cfg.state_dir) / "source_progress.jsonl")
    best = None
    for lane, ln in cfg.lanes.items():
        if cfg.page_market(lane) != market:
            continue
        try:
            plan = build_plan(fac, reports, market, [ln.origin_import])
        except PlanError:
            continue
        n = choose(plan, slow=prog.slow_map(market), evaluated=prog.evaluated(market), needed=cfg.needed_sources()).selectable
        best = n if best is None else max(best, n)
    return best


def _cmd_plan_compare(a, factory=None, **_) -> int:
    """（読み取りのみ）従来の取得対象と、効率による後ろ回し・候補不足の戻し・未評価枠を入れた取得対象を比べる。DB・台帳・状態は書かない。"""
    from acquisition.plan import PlanError, build_plan
    from origin_selection.report_import import ReportImportService
    from workflow.progress import SourceProgress
    from workflow.source_select import compare, format_compare

    cfg = load_config(a.config)
    fac = factory or _readonly_factory()
    reports = ReportImportService(fac, None, actor="workflow-plan-compare")
    prog = SourceProgress(Path(cfg.state_dir) / "source_progress.jsonl")
    for lane, ln in cfg.lanes.items():
        if a.market != "ALL" and lane != a.market:
            continue
        pm = cfg.page_market(lane)
        try:
            plan = build_plan(fac, reports, pm, [ln.origin_import])
        except PlanError as exc:
            print(f"[{lane}] 計画を作れません: {exc}")
            continue
        slow = prog.slow_map(pm)
        sim = getattr(a, "simulate_shortage", None)
        cmp = compare(plan, slow, prog.evaluated(pm), needed=sim if sim else cfg.needed_sources(), reserve=cfg.completion.reserve_unevaluated)
        print("\n".join(format_compare(pm, cmp, slow, simulated_needed=sim or None)))
    return 0


def _cmd_reconcile(a, factory=None, confirm=None, **_) -> int:
    from tool_submission.ledger import SubmissionLedger
    from workflow.progress import SourceProgress, resume_snapshot
    from workflow.quarantine import FILE_NAME, NeedsHuman
    from workflow.reconcile import apply_reconcile, format_scan, scan

    store = WorkflowStore(a.state_dir)
    ledger = SubmissionLedger(Path(a.submission_dir) / "ledger.jsonl")
    if not a.apply:
        print("\n".join(format_scan(scan(store, ledger, explain=a.explain))))
        print("（dry-runです。何も書いていません。整理するなら --apply）")
        return 0
    if confirm is None:
        if not sys.stdin.isatty():
            print("[中止] --apply は対話端末でのy/n確認が必要です")
            return 2
        confirm = lambda q: input(q).strip().lower() == "y"  # noqa: E731
    fac = factory
    snap = (lambda m, sid: resume_snapshot(fac, m, sid)) if fac is not None else None
    try:
        out = apply_reconcile(store, ledger, SourceProgress(Path(a.state_dir) / "source_progress.jsonl"), snapshot=snap, confirm=confirm,
                              include_d=a.include_d, needs=NeedsHuman(Path(a.state_dir) / FILE_NAME))
    except RuntimeError as exc:
        print(f"[中止] {exc}")
        return 2
    print("\n".join(format_scan(out["scan"], apply=True)))
    if out.get("declined"):
        print("確認が取れなかったので何も変更していません")
        return 2
    for c in out["changed"]:
        print(f"  整理: {c['workflow']} → {c['overall_after']}（元の状態を退避: {c['backup']}）")
    return 0


def _cmd_needs_human(a, **_) -> int:
    """人の判断待ち（reconcile --apply --include-d で取り分けたもの）の一覧・解決・戻し。投入台帳・取得履歴は変更しない。"""
    from datetime import datetime, timezone

    from workflow.quarantine import FILE_NAME, NeedsHuman, format_items
    from workflow.reconcile import reopen

    needs = NeedsHuman(Path(a.state_dir) / FILE_NAME)
    if a.resolve:
        try:
            needs.resolve(a.resolve, a.decision, a.note or "")
        except KeyError as exc:
            print(f"[中止] {exc}")
            return 2
        print(f"解決として記録しました: {a.resolve} ({a.decision})")
        return 0
    if a.reopen:
        try:
            ch = reopen(WorkflowStore(a.state_dir), needs, a.reopen, a.note or "", allow_duplicate=a.allow_duplicate)
        except (RuntimeError, KeyError) as exc:
            print(f"[中止] {exc}")
            return 2
        print(f"取り分けを戻しました: {a.reopen} → " + (", ".join(f"{c['workflow']}:{c['task']}={c['status']}" for c in ch) or "戻す工程なし"))
        return 0
    print("\n".join(format_items(needs.open_items(), datetime.now(timezone.utc))))
    return 0


def _cmd_backup(a, **_) -> int:
    info = WorkflowStore(a.state_dir).engine_lock_info()
    if info and not a.while_running:
        print(f"[中止] watch / run / resume が稼働中です（pid={info.get('pid')}・{info.get('who')}）。整合したバックアップのため、Ctrl+Cで止めてから取ってください"
              "（稼働中に取るなら --while-running。台帳の最後の1行などが途中の可能性があります）")
        return 2
    r = create_backup(a.state_dir, a.submission_dir, a.out)
    print(f"バックアップを作成しました: {r['zip']}（{r['files']}ファイル、sha256目録つき）。Gitに入れないでください")
    return 0


def _cmd_restore(a, **_) -> int:
    r = restore_backup(a.zip, a.state_dir, a.submission_dir, force=a.force)
    print(f"復元しました: {r['restored']}ファイル（退避{r['moved_aside']}件）。`status` / `resume` で続きから確認できます")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m workflow.cli")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        return sp

    sp = add("init-config", _cmd_init, "設定の例をコピーする")
    sp.add_argument("--out", default="config/workflow.json")
    for n, fn, h in (("plan", _cmd_plan, "実行対象・予定・上限を表示（外部アクセス・DB更新なし）"), ("run", _cmd_run, "計画に従って実行")):
        sp = add(n, fn, h)
        sp.add_argument("--config", default="config/workflow.json")
    sp = add("watch", _cmd_watch, "常駐: 状態確認→未完了の再開→次の候補の処理→待機→再確認（Ctrl+Cで停止。PC停止中は動かない）")
    sp.add_argument("--config", default="config/workflow.json")
    sp.add_argument("--max-cycles", type=int, default=None, help="（検証用）この回数の確認で終了する")
    sp = add("fetch-source", _cmd_fetch_source, "指定した1出典を1回だけ取得する（日次のページ使用量を通す。--max-pagesは新しく保存して進むページ数で、overlapは別枠）")
    sp.add_argument("--config", default="config/workflow.json")
    sp.add_argument("--market", required=True, choices=["US", "JP"])
    sp.add_argument("--source-id", required=True, type=int)
    sp.add_argument("--max-pages", type=int, default=2, help="新しく保存して進む最大ページ数（続きの前の重なり確認(overlap)は含まない別枠）")
    sp.add_argument("--total-cap", type=int, default=None, help="重なり確認を含む実アクセスの総数の上限（指定すると max-pages をそれに合わせて減らす）")
    sp = add("stop", _cmd_stop, "稼働中のwatchに安全な区切りでの停止を依頼する")
    sp.add_argument("--state-dir", default="exports/workflow")
    sp = add("yield", _cmd_yield, "取得・投入の成果（新規ASIN・投入数・既存の出品成果）を表示（読み取りのみ）")
    sp.add_argument("--config", default="config/workflow.json")
    sp.add_argument("--submission-dir", default="exports/tool_submission")
    sp = add("status", _cmd_status, "工程別の状態")
    sp.add_argument("--id", default=None)
    sp.add_argument("--state-dir", default="exports/workflow")
    sp = add("resume", _cmd_resume, "保存済み状態から再開")
    sp.add_argument("--config", default="config/workflow.json", help="上限・間隔などは現在の設定を使う（市場・起点importは保存済みのもの）")
    sp.add_argument("--id", default=None)
    sp.add_argument("--state-dir", default="exports/workflow")
    sp.add_argument("--allow-duplicate", action="append", help="重複候補として保留された投入キーを、意図した再投入として許可する")
    sp = add("outcome", _cmd_outcome, "成果の表示（読み取りのみ）: 取得・初見ASIN・投入・持ち越し・出典別の効率")
    sp.add_argument("--market", required=True, choices=["US", "JP", "ALL"])
    sp.add_argument("--since", default=None, help="開始（日付/日時。タイムゾーン無しはJST）。既定は--untilの24時間前")
    sp.add_argument("--until", default=None, help="終了（含まない）。既定は現在")
    sp.add_argument("--top", type=int, default=15, help="出典別に表示する件数（ページ数の多い順）")
    sp.add_argument("--config", default=None, help="指定すると、選択可能な出典数（後退中でない）と取り直し見送りの影響を実際の候補数で見積もる")
    sp.add_argument("--state-dir", default="exports/workflow")
    sp.add_argument("--submission-dir", default="exports/tool_submission")
    sp = add("plan-compare", _cmd_plan_compare, "（読み取りのみ）従来の取得対象と新しい取得対象の比較・選択可能な出典数")
    sp.add_argument("--config", default="config/workflow.json")
    sp.add_argument("--market", default="ALL", choices=["US", "JP", "ALL"])
    sp.add_argument("--simulate-shortage", type=int, default=None, metavar="N", help="（読み取りのみ）必要数をNとして判定し、選択可能数がNを下回るときの補充結果（戻す後退中の出典）を表示する")
    sp = add("needs-human", _cmd_needs_human, "人の判断待ちの一覧（既定）/ --resolve で解決を記録 / --reopen で取り分けを戻す")
    sp.add_argument("--state-dir", default="exports/workflow")
    sp.add_argument("--resolve", default=None, metavar="ID")
    sp.add_argument("--decision", default="OTHER", choices=["ALLOWED", "ALREADY_REGISTERED", "WITHDRAWN", "OTHER"])
    sp.add_argument("--reopen", default=None, metavar="ID")
    sp.add_argument("--allow-duplicate", action="store_true", help="--reopenと一緒に: 意図した再投入として許可も追記する")
    sp.add_argument("--note", default=None)
    sp = add("reconcile", _cmd_reconcile, "未完了workflowの整理。既定はdry-run（何も書かない）。--applyはy/n確認が必要")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--explain", action="store_true", help="dry-run: REPEAT_OF_EARLIER_CYCLEの各キーについて、台帳の状態・登録済みの実行とのASIN重なり・未登録のまま残る件数を表示")
    sp.add_argument("--include-d", action="store_true", help="--applyで、Dのうち取り分けてよい理由の投入工程を needs_human.jsonl に記録して閉じる（登録済みとは扱わない）")
    sp.add_argument("--state-dir", default="exports/workflow")
    sp.add_argument("--submission-dir", default="exports/tool_submission")
    for n, fn, h in (("backup", _cmd_backup, "状態・台帳・マニフェストをまとめて保存"), ("restore", _cmd_restore, "検証して復元")):
        sp = add(n, fn, h)
        sp.add_argument("--state-dir", default="exports/workflow")
        sp.add_argument("--submission-dir", default="exports/tool_submission")
        if n == "backup":
            sp.add_argument("--out", required=True)
            sp.add_argument("--while-running", action="store_true", help="稼働中でも取る（整合は保証されない）")
        else:
            sp.add_argument("--zip", required=True)
            sp.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None, *, factory=None, ports=None, confirm=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a, factory=factory, ports=ports, **({"confirm": confirm} if a.func is _cmd_reconcile else {}))
    except (ConfigError, BackupError, StateCorrupt) as exc:
        print(f"[中止] {exc}")
        return 2
    except WorkflowBusy as exc:
        print(f"[中止] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 未適用のマイグレーションは案内して終了（それ以外は通常の例外として送出）
        from db.schema_guard import explain

        msg = explain(exc)
        if msg is None:
            raise
        print(f"[中止] {msg}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
