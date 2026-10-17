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
from workflow.store import DONE_STATES, State, StateCorrupt, WorkflowBusy, WorkflowStore

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
        if t.kind == "submit" and t.status == "DONE":
            continue
        lines.append(f"    - {t.id} [{t.lane}] {mark}" + (f": {t.detail}" if t.detail else ""))
        if t.status in ("WAITING", "HELD", "UNKNOWN", "FAILED"):
            actions.append(f"{t.id}（{t.lane}）: {t.detail}")
    held = [(t.lane, h) for t in st.tasks.values() if t.kind == "discover" for h in t.result.get("held", [])]
    for lane, h in held:
        lines.append(f"    ! [{lane}] Amazon小売かの判定待ち: {h['key']}（{(h.get('detail') or '')[:80]}）— 人が判断。登録していません")
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


def _print_status(st: State) -> int:
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
    st = eng.new_state()
    print(f"workflow {st.workflow_id} を開始します（設定を保存済み。以後の再開はこの設定で行います）")
    with eng.store.lock(st.workflow_id):
        eng.drive()
    return _print_status(eng.st)


def _cmd_resume(a, factory=None, ports=None) -> int:
    store = WorkflowStore(a.state_dir)
    wid = _latest(store, a.id)
    st = store.load(wid)
    cfg = config_from_dict(st.config)  # 保存した設定スナップショットで再開する（設定ファイルの後からの変更は反映しない）
    eng = _engine(cfg, factory, ports)
    eng.store, eng.st = store, st
    for k in a.allow_duplicate or []:
        if k not in st.allow_duplicates:
            st.allow_duplicates.append(k)
    with store.lock(wid):
        eng.prepare_resume()
        eng.drive()
    return _print_status(eng.st)


def _cmd_status(a, **_) -> int:
    store = WorkflowStore(a.state_dir)
    return _print_status(store.load(_latest(store, a.id)))


def _cmd_backup(a, **_) -> int:
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
    sp = add("status", _cmd_status, "工程別の状態")
    sp.add_argument("--id", default=None)
    sp.add_argument("--state-dir", default="exports/workflow")
    sp = add("resume", _cmd_resume, "保存済み状態から再開")
    sp.add_argument("--id", default=None)
    sp.add_argument("--state-dir", default="exports/workflow")
    sp.add_argument("--allow-duplicate", action="append", help="重複候補として保留された投入キーを、意図した再投入として許可する")
    for n, fn, h in (("backup", _cmd_backup, "状態・台帳・マニフェストをまとめて保存"), ("restore", _cmd_restore, "検証して復元")):
        sp = add(n, fn, h)
        sp.add_argument("--state-dir", default="exports/workflow")
        sp.add_argument("--submission-dir", default="exports/tool_submission")
        if n == "backup":
            sp.add_argument("--out", required=True)
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
