"""統合ツール投入の計画・台帳・照合のCLI（統合ツールには接続しない）。

    python -m tool_submission.cli plan --fetch-market US --run-id 82 [--tool-country jp|us] [--dry-run]
    python -m tool_submission.cli ledger
    python -m tool_submission.cli reconcile --key <投入キー> --log-html <保存したログ画面.html>

`plan`はDBを読み取るだけで、入力ファイル（出典ごと・業務方向ごと）とマニフェストを`exports/tool_submission/`に作り、台帳(JSONL)に
計画を残す。ツールの国設定(`--tool-country`)が無い計画はDRAFT（投入できない）。`reconcile`は、利用者が手動で投入した後、保存した
ログ画面HTMLから統合ツールの実行ID（`data-seq`）を探して台帳に確定する（メモ・種別・国設定が一致する、他の投入に使われていない行が
ちょうど1件のときだけ）。確定後は既存の`tool_intake.cli ingest-log`が`tool_execution_link`へ記録する。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from db.session import SessionFactory
from tool_intake.csv_utils import open_text_auto
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan
from tool_submission.reconcile import match_new_entries, parse_log_entries

DEFAULT_DIR = Path("exports") / "tool_submission"


def _cmd_plan(a: argparse.Namespace) -> int:
    out_dir = Path(a.out_dir) if a.out_dir else DEFAULT_DIR
    try:
        items, files = build_items(SessionFactory.for_app_role(), a.fetch_market, a.run_id, tool_country=a.tool_country, out_dir=out_dir,
                                   direction_override=a.direction)
    except SubmissionError as exc:
        print(f"[中止] {exc}")
        return 2
    for it in items:
        print(f"投入キー={it.key} [{it.status}] 取得run={it.collector_run_id}（取得先市場={it.fetch_market}、結果={it.run_outcome}"
              f"{'・一部の取得結果' if it.input_is_partial else ''}）出典={it.source_type}:{it.source_key}(id={it.source_id}) "
              f"業務方向={it.direction}（メモ接頭辞の市場={it.business_market}）メモ={it.memo!r} ツールの国設定={it.tool_country or '未確定'}")
        print(f"    入力: ASIN{it.asin_count}件（取得{it.total_rows}行・重複除去{it.duplicate_rows_removed}）sha256={it.input_sha256[:16]}… → {it.input_file}")
        if it.origin:
            print(f"    起点: {it.origin['market']} import={it.origin['import_id']} {it.origin['asin']} 注文{it.origin['units']}点")
        if it.other_directions:
            print(f"    同じ取得runを別の方向でも使う: {it.other_directions}（別の入力・別のメモ）")
    if a.dry_run:
        print("（--dry-run: ファイルも台帳も書きません）")
        return 0
    ledger = SubmissionLedger(Path(a.ledger) if a.ledger else out_dir / "ledger.jsonl")
    for it, how in write_plan(items, files, ledger):
        print(f"  {it.key}: 台帳 {how}")
    if any(it.status == "DRAFT" for it in items):
        print("[注意] ツールの国設定が未確定のため、投入できません（--tool-country jp|us を確認してから再度planを実行）")
    return 0


def _cmd_ledger(a: argparse.Namespace) -> int:
    ledger = SubmissionLedger(Path(a.ledger) if a.ledger else DEFAULT_DIR / "ledger.jsonl")
    states = ledger.all_states()
    for key, st in states.items():
        m = st.manifest or {}
        print(f"{key} {st.state} 試行{st.attempts}回 run={m.get('collector_run_id')} {m.get('direction')} {m.get('memo')} "
              f"ツール実行ID={st.tool_run_id or '-'}")
    if not states:
        print("台帳は空です")
    return 0


def _cmd_reconcile(a: argparse.Namespace) -> int:
    ledger = SubmissionLedger(Path(a.ledger) if a.ledger else DEFAULT_DIR / "ledger.jsonl")
    st = ledger.state(a.key)
    if st.state == "NONE" or not st.manifest:
        print("[中止] 台帳に投入計画がありません")
        return 2
    if st.state == "CONFIRMED":
        print(f"確定済みです: ツール実行ID={st.tool_run_id}")
        return 0
    entries = parse_log_entries(open_text_auto(Path(a.log_html)))
    others = {int(s.tool_run_id) for k, s in ledger.all_states().items() if k != a.key and s.state == "CONFIRMED" and s.tool_run_id}
    before = set(st.before_run_ids) if st.before_run_ids else None  # 自動投入の基準があればそれを使う（手動投入は基準なし）
    m = st.manifest
    d = match_new_entries(entries, m["memo"], m.get("tool_country"), before_run_ids=before, exclude_run_ids=others)
    if d.kind == "CONFIRMED":
        ledger.append(a.key, "CONFIRMED", tool_run_id=d.tool_run_id, via="log_html")
        print(f"確定: ツール実行ID={d.tool_run_id}（メモ={m['memo']!r}）。続けて tool_intake.cli ingest-log で記録できます")
    elif d.kind == "AMBIGUOUS":
        ledger.append(a.key, "AMBIGUOUS", candidates=list(d.candidates))
        print(f"[曖昧] 該当しそうな実行が複数あります: {list(d.candidates)}（二重投入の疑い。確定しません）")
        return 3
    else:
        print("該当する実行がログ画面に見つかりません（確定しません）")
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tool_submission.cli")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("plan", help="取得runから入力ファイルと投入計画を作る（DB読み取りのみ）")
    sp.add_argument("--fetch-market", required=True, choices=["US", "JP"], help="ASINを取得したAmazonの市場（collector_runの市場）")
    sp.add_argument("--run-id", required=True, type=int)
    sp.add_argument("--tool-country", default=None, choices=["jp", "us"], help="統合ツールがASINを調べる国（data-country）。未指定=DRAFT")
    sp.add_argument("--direction", default=None, choices=["EXPORT", "IMPORT"], help="出典の経路に方向が無い場合だけ明示する")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--ledger", default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_plan)
    sp = sub.add_parser("ledger", help="投入台帳の状態を表示する")
    sp.add_argument("--ledger", default=None)
    sp.set_defaults(func=_cmd_ledger)
    sp = sub.add_parser("reconcile", help="保存したログ画面HTMLから統合ツールの実行IDを探して台帳に確定する")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-html", required=True)
    sp.add_argument("--ledger", default=None)
    sp.set_defaults(func=_cmd_reconcile)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
