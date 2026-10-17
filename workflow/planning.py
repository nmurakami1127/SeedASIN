"""plan: 外部アクセス・DB更新をせず、実行対象と予定操作・上限を表示する（DBは読み取りのみ）。

探索後に初めて分かる値（発見される出典の数・取得ASIN数・投入件数）は**確定値ではなく見積り・上限**として表示する。
"""
from __future__ import annotations

from pathlib import Path

from acquisition.discovery import DiscoveryError, DiscoveryLimits, load_own_account_ids
from acquisition.plan import PlanError, validate_origin_imports
from tool_submission.ledger import SubmissionLedger
from tool_submission.runner import PENDING_STATES
from workflow.config import OPPOSITE, WorkflowConfig


def build_plan_text(engine) -> tuple[list[str], list[str]]:
    """(表示行, 実行できない理由)。"""
    cfg: WorkflowConfig = engine.cfg
    lines: list[str] = []
    blockers: list[str] = []
    lim, tot = cfg.limits, cfg.totals
    lines.append("== workflow plan（外部アクセス・DB更新なし。DBは読み取りのみ）==")
    lines.append(f"設定: sha256={cfg.sha256()[:12]}…  ブランド取得={'ON' if cfg.brand_enabled else 'OFF（未検証。SELLERから開始）'}  "
                 f"反対側展開={'ON' if cfg.cross.enabled else 'OFF'}  反対側で発見した出典の自動取得={'ON' if cfg.cross.fetch_discovered else 'OFF'}")
    lines.append(f"工程別の上限: 起点ASIN{lim.origin_asins}/市場 ・ 発見出典{lim.sources_per_origin}/起点 ・ 取得{lim.pages_per_source}ページ/出典 ・ "
                 f"反対側展開{lim.cross_asins}ASIN/市場 ・ 投入1回{cfg.tool_max_asins}ASIN以下")
    lines.append(f"実行全体の上限: 取得ページ合計{tot.max_total_pages} ・ 新規出典合計{tot.max_new_sources} ・ 投入ASIN合計{tot.max_submit_asins}")
    max_sources = 0
    for lane, ln in cfg.lanes.items():
        pm = cfg.page_market(lane)
        lines.append(f"\n[{lane}] 起点レポート import_id={ln.origin_import}（明示）→ 商品ページ・出典取得の市場={pm} 経路={ln.routes}")
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
            sel = engine.svc.select_origins(ln.origin_import, pm, DiscoveryLimits(lim.origin_asins, lim.sources_per_origin, lim.cross_asins,
                                                                                   lim.cross_sources_per_asin))
        except (DiscoveryError, PlanError) as exc:
            blockers.append(f"[{lane}] {exc}")
            lines.append(f"  [実行不可] {exc}")
            continue
        for pk in sel.selected:
            c = pk.candidate
            how = "商品ページを取得" if pk.mode == "FETCH" else f"保存済みの調査結果を再利用(run={pk.stored_run_id})"
            lines.append(f"  起点 #{c.rank} {c.asin} 注文{c.units_sum}点 セッション{c.sessions_sum} → {how}")
        for asin, state, until in sel.skipped[:5]:
            lines.append(f"  見送り {asin}: {state}" + (f"（{until:%Y-%m-%d %H:%M} UTCまで）" if until else ""))
        n_src = len(sel.selected) * lim.sources_per_origin
        max_sources += n_src
        lines.append(f"  見積り・上限: 発見出典 最大{n_src}件 / 取得 最大{n_src * lim.pages_per_source}ページ"
                     + (f" / 反対側展開 最大{lim.cross_asins}ASIN（{OPPOSITE[pm]}の商品ページ）" if cfg.cross.enabled else ""))
    lines.append(f"\n合計の見積り・上限: 新規出典 最大{min(max_sources, tot.max_new_sources)}件 / 取得ページ 最大"
                 f"{min(max_sources * lim.pages_per_source, tot.max_total_pages)} / 投入ASIN 最大{tot.max_submit_asins}"
                 "（発見・取得の後に確定。ここでは確定値ではありません）")
    ledger = SubmissionLedger(Path(cfg.submission_dir) / "ledger.jsonl")
    pend = {k: s.state for k, s in ledger.all_states().items() if s.state in PENDING_STATES}
    if pend:
        lines.append(f"\n[注意] 未完了の投入があります: {pend}（結果不明・曖昧・メモ未保存のものが解決するまで、新しい投入は保留になります）")
    lines.append("\n運用上の前提: 統合ツールへの同時投入が無いこと（ローカルのロックは同じPC内のプロセスしか防げません。別PC・別ブラウザからの投入は防げません）。"
                 "\n人の操作が必要になる場面: 統合ツールのログイン / CAPTCHA / 実行IDの対応不明 / Amazon小売かの判定。")
    lines.append("統合ツールの処理完了待ち・出力CSVの検証/取込は、このworkflowに含みません。")
    return lines, blockers
