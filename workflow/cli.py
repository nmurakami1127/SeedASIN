"""ひとつの実行入口。

    python -m workflow.cli init-config [--out config/workflow.json]      # 例（config/workflow.example.json）をコピー。import_idを書き換える
    python -m workflow.cli plan   --config config/workflow.json           # 外部アクセス・DB更新なし。対象・予定・上限を表示
    python -m workflow.cli run    --config config/workflow.json           # 計画に従って実行（人の操作が必要な所で待機/保留）
    python -m workflow.cli status [--id wf-…]                             # 工程別の完了・待機・保留・失敗・結果不明
    python -m workflow.cli resume [--id wf-…] [--allow-duplicate <投入キー>]   # 保存済み状態から再開（完了工程は繰り返さない）
    python -m workflow.cli backup  --out backups/wf.zip                    # workflow状態・投入台帳・マニフェストをまとめて保存
    python -m workflow.cli restore --zip backups/wf.zip [--force]          # 検証して復元

実行のたびにセレクタ・ログJSON・options JSON・内部IDを入力しない。設定ファイルはGit管理しない（import_id等の運用値）。
終了コード: 0=完了 / 2=設定・実行不可 / 3=人の操作が必要（待機・保留・結果不明）または失敗あり（resumeで再開）。
"""
from __future__ import annotations

import argparse
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
            lab = {"REUSED": "再利用(保存済みの取得結果)", "FETCHED": "新規取得", "HELD": "保留", "CAPTCHA": "待機(CAPTCHA)",
                   "FAILED": "失敗", "SKIPPED_AT_CLAIM": "スキップ"}.get(r["mode"], r["mode"])
            add(lab + "・上限打切り" if r.get("truncated") else lab)  # 上限打切りと取得完了を区別する
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
    done_sub = [t for t in st.tasks.values() if t.kind == "submit" and t.status == "DONE"]
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


def main(argv: list[str] | None = None, *, factory=None, ports=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a, factory=factory, ports=ports)
    except (ConfigError, BackupError, StateCorrupt) as exc:
        print(f"[中止] {exc}")
        return 2
    except WorkflowBusy as exc:
        print(f"[中止] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
