"""plan: 外部アクセス・DB更新をせず、次に選ばれる候補・再利用・保留・上限を表示する（DBと投入台帳・workflow状態の読み取りのみ）。

実行と**同じ選択条件**（`Engine.origin_selection`・`discover_limits`）を使う。状態を進めない・日次の使用枠も確保しない。
探索後に初めて分かる値（発見される出典の数・取得ASIN数・投入件数）は確定値ではなく見積り・上限として表示する。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from acquisition.discovery import DiscoveryError, load_own_account_ids
from acquisition.plan import PlanError, validate_origin_imports
from tool_submission.ledger import SubmissionLedger
from tool_submission.runner import PENDING_STATES
from workflow.config import OPPOSITE, WorkflowConfig
from workflow.daily_usage import JST, DailyUsage, jst_day, next_jst_midnight

MODE_JA = {"ordered_only": "注文ありのみ", "ordered_then_zero": "注文あり→注文0点", "zero_only": "注文0点のみ"}


def usage_lines(cfg: WorkflowConfig, usage: DailyUsage, session_since: str | None = None) -> list[str]:
    now = datetime.now(timezone.utc)
    u, d = usage.used(), cfg.daily
    cap = lambda v: "無制限" if v is None else str(v)  # noqa: E731
    lines = [f"  本日(JST {jst_day(now)}、次の区切り {next_jst_midnight(now).astimezone(JST):%m-%d %H:%M} JST)の使用量: "
             f"Amazon新規取得 {u.get('pages', 0)}/{cap(d.max_pages)}ページ ・ 統合ツール新規投入 輸出 {u.get('submit:EXPORT', 0)}/{cap(d.max_new_submissions_per_direction)}件・"
             f"輸入 {u.get('submit:IMPORT', 0)}/{cap(d.max_new_submissions_per_direction)}件 ・ 投入ASIN {u.get('asins', 0)}/{cap(d.max_submit_asins)}件"]
    if session_since:
        s = usage.used_since(session_since)
        lines.append(f"  今回(起動以降)の使用量: ページ{s.get('pages', 0)} ・ 輸出{s.get('submit:EXPORT', 0)}件・輸入{s.get('submit:IMPORT', 0)}件 ・ ASIN{s.get('asins', 0)}件")
    return lines


def build_plan_text(engine) -> tuple[list[str], list[str]]:
    """(表示行, 実行できない理由)。"""
    cfg: WorkflowConfig = engine.cfg
    lines: list[str] = []
    blockers: list[str] = []
    lim, tot, dly = cfg.limits, cfg.totals, cfg.daily
    lines.append("== workflow plan（外部アクセス・DB更新なし。状態を進めず、使用枠も確保しません）==")
    lines.append(f"設定: プロファイル={cfg.profile}  sha256={cfg.sha256()[:12]}…  ブランド取得={'ON' if cfg.brand_enabled else 'OFF（未検証。SELLERから開始）'}  "
                 f"反対側展開={'ON' if cfg.cross.enabled else 'OFF'}  反対側で発見した出典の自動取得={'ON' if cfg.cross.fetch_discovered else 'OFF'}")
    lines.append(f"採用された既定値: 商品ページの再調査間隔={cfg.page_revisit_days}日（出典の取得間隔は acquisition の設定。別） ・ 保存済み取得結果の鮮度={cfg.result_max_age_days}日 ・ "
                 f"watch再確認={cfg.watch.interval_minutes}分 ・ 失敗の冷却={cfg.watch.failure_retry_minutes}分")
    lines.append(f"1回の上限: 起点{lim.origin_asins}/市場 ・ 発見出典{lim.sources_per_origin}/起点 ・ 取得{lim.pages_per_source}ページ/出典 ・ "
                 f"反対側展開{lim.cross_asins}ASIN/市場 ・ 投入1回{cfg.tool_max_asins}ASIN以下 ・ 取得ページ合計{tot.max_total_pages} ・ 新規出典合計{tot.max_new_sources} ・ "
                 f"投入ASIN合計{tot.max_submit_asins} ・ 新規投入{tot.max_new_submissions_per_direction}件/方向")
    cap = lambda v: "無制限" if v is None else str(v)  # noqa: E731
    lines.append(f"日次上限(watch全体・JST): Amazon新規取得 {cap(dly.max_pages)}ページ ・ 新規投入 {cap(dly.max_new_submissions_per_direction)}件/方向 ・ 投入ASIN {cap(dly.max_submit_asins)}件")
    lines += usage_lines(cfg, engine.usage)
    max_sources = 0
    for lane, ln in cfg.lanes.items():
        pm = cfg.page_market(lane)
        lines.append(f"\n[{lane}] 起点レポート import_id={ln.origin_import}（明示） 選択モード={ln.selection_mode}（{MODE_JA[ln.selection_mode]}） "
                     f"→ 商品ページ・出典取得の市場={pm} 経路={ln.routes}")
        try:
            info = validate_origin_imports(engine.reports, [ln.origin_import])[0]
            if info.marketplace != lane:
                raise PlanError(f"import_id={ln.origin_import} の市場は{info.marketplace}です")
            lines.append(f"  レポート: ACTIVE・市場{info.marketplace}・方向{info.business_direction}・期間{info.period_start}〜{info.period_end}")
        except PlanError as exc:
            blockers.append(f"[{lane}] {exc}")
            lines.append(f"  [実行不可] {exc}")
            continue
        own = load_own_account_ids(engine.factory, pm)
        if not own:
            blockers.append(f"[{lane}] 自社セラーID({pm})が未設定")
            lines.append(f"  [実行不可] 自社セラーID({pm})が未設定（own-account-add で設定）")
        else:
            lines.append(f"  自社セラーID({pm}): {len(own)}件（発見から除外）")
        try:
            sel = engine.origin_selection(lane, engine.discover_limits(tot.max_new_sources, tot.max_total_pages))
        except (DiscoveryError, PlanError) as exc:
            blockers.append(f"[{lane}] {exc}")
            lines.append(f"  [実行不可] {exc}")
            continue
        for pk in sel.selected:
            c = pk.candidate
            how = "商品ページを取得（新規）" if pk.mode == "FETCH" else f"保存済みの調査結果から続き（{sel.reasons.get(pk.asin, '未処理あり')}）。ページへは再アクセスしない"
            lines.append(f"  次の候補 #{c.rank} {c.asin} 注文{c.units_sum}点 セッション{c.sessions_sum} 最大獲得率{c.buybox_rate_max_pct}% → {how}")
        if not sel.selected:
            lines.append("  次の候補: なし（実行可能な候補がありません。watchなら外部を開かずに待機します）")
        lines.append(f"  調べた候補{sel.scanned}件: 探索済み{len(sel.done)}件（再探索まで枠を使わない）・保留/冷却/実行中など{len(sel.skipped)}件")
        for asin, until in sel.done[:3]:
            lines.append(f"    探索済み {asin}" + (f"（再探索可能 {until.astimezone(JST):%m-%d %H:%M} JST）" if until else ""))
        for asin, state, until in sel.skipped[:3]:
            lines.append(f"    見送り {asin}: {state}" + (f"（{until.astimezone(JST):%m-%d %H:%M} JSTまで）" if until else ""))
        n_src = len(sel.selected) * lim.sources_per_origin
        max_sources += n_src
        lines.append(f"  見積り・上限: 発見出典 最大{n_src}件 / 取得 最大{n_src * lim.pages_per_source}ページ"
                     + (f" / 反対側展開 最大{lim.cross_asins}ASIN（{OPPOSITE[pm]}の商品ページ）" if cfg.cross.enabled else ""))
    lines.append(f"\n合計の見積り・上限（1回）: 新規出典 最大{min(max_sources, tot.max_new_sources)}件 / 取得ページ 最大"
                 f"{min(max_sources * lim.pages_per_source, tot.max_total_pages)}（発見・取得の後に確定。ここでは確定値ではありません）")
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    pend = {k: s.state for k, s in ledger.all_states().items() if s.state in PENDING_STATES}
    if pend:
        lines.append(f"\n[注意] 未完了の投入があります: {pend}（結果不明・曖昧・メモ未保存のものが解決するまで、新しい投入は保留になります）")
    lines.append("\n運用上の前提: 統合ツールへの同時投入が無いこと（ローカルのロックは同じPC内のプロセスしか防げません。別PC・別ブラウザからの投入は防げません）。"
                 "\n人の操作が必要になる場面: 統合ツールのログイン / CAPTCHA / 実行IDの対応不明 / Amazon小売かの判定。PCが停止中・スリープ中はwatchも動きません。")
    lines.append("統合ツールの処理完了待ち・出力CSVの検証/取込は、このworkflowに含みません。")
    return lines, blockers
