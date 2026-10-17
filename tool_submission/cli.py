"""統合ツール投入の計画・台帳・照合のCLI。

    python -m tool_submission.cli plan --fetch-market US --run-id 82 [--dry-run]
    python -m tool_submission.cli ledger
    # 手動で投入する場合（画面の手順は docs/DEVIATIONS.md #60）
    python -m tool_submission.cli baseline  --key <投入キー> --log-html <送信前に保存したログ画面.html>
    python -m tool_submission.cli reconcile --key <投入キー> --log-html <送信後に保存したログ画面.html>
    python -m tool_submission.cli resolve   --key <投入キー> --run-id <実行ID>        # 新規行が複数のとき、人が選ぶ
    python -m tool_submission.cli link-check --key <投入キー>                        # ingest-log後、対応記録済みを確認（DB読み取りのみ）
    python -m tool_submission.cli verify-output --key <投入キー> --csv <統合ツールの出力CSV>
    python -m tool_submission.cli submit --key <投入キー> --selectors <確認済みセレクタのJSON>   # 自動送信（実画面は未検証・初回は1件ずつ）

`plan`はDBを読み取るだけで、入力ファイル（出典ごと・業務方向ごと・5,000件以下ごと）とマニフェストを`exports/tool_submission/`に作り、
台帳(JSONL)に計画を残す。**ASINリスト入力に国指定は無い**ので国設定は不要。メモはログ画面で投入の後に付けるため、実行IDは
「送信前のログ一覧（基準）に無い新しいasin_list行」から特定する（メモ一致では探さない）。自動送信（`submit`）は、利用者が入力画面の
セレクタを確認して`--selectors`で渡すまで実行できない。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from db.session import SessionFactory
from tool_intake.csv_utils import open_text_auto
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan
from tool_submission.reconcile import identify_new_entry, parse_log_entries, verify_output

DEFAULT_DIR = Path("exports") / "tool_submission"


def _ledger(a) -> SubmissionLedger:
    return SubmissionLedger(Path(a.ledger) if getattr(a, "ledger", None) else DEFAULT_DIR / "ledger.jsonl")


def _cmd_plan(a: argparse.Namespace) -> int:
    out_dir = Path(a.out_dir) if a.out_dir else DEFAULT_DIR
    try:
        items, files = build_items(SessionFactory.for_app_role(), a.fetch_market, a.run_id, out_dir=out_dir, direction_override=a.direction)
    except SubmissionError as exc:
        print(f"[中止] {exc}")
        return 2
    for it in items:
        part = f" 分割{it.chunk_index}/{it.chunk_count}" if it.chunk_count > 1 else ""
        print(f"投入キー={it.key} 取得run={it.collector_run_id}（取得先市場={it.fetch_market}、結果={it.run_outcome}"
              f"{'・一部の取得結果' if it.input_is_partial else ''}）出典={it.source_type}:{it.source_key}(id={it.source_id}) "
              f"業務方向={it.direction}（メモ接頭辞の市場={it.business_market}）メモ（投入後にログ画面で付ける）={it.memo!r}{part}")
        print(f"    入力: ASIN{it.asin_count}件（取得{it.total_rows}行・重複除去{it.duplicate_rows_removed}）sha256={it.input_sha256[:16]}… → {it.input_file}")
        if it.origin:
            print(f"    起点: {it.origin['market']} import={it.origin['import_id']} {it.origin['asin']} 注文{it.origin['units']}点")
        if it.prior_tool_runs:
            print(f"    [情報] 同じ業務方向・同じ文字列の統合ツール実行が既に{len(it.prior_tool_runs)}件あります: {it.prior_tool_runs}"
                  "（照合は送信前のログ一覧を基準にするので、これらを理由に除外はしません）")
        if it.other_directions:
            print(f"    同じ取得runを別の方向でも使う: {it.other_directions}（別の入力・別のメモ）")
    if a.dry_run:
        print("（--dry-run: ファイルも台帳も書きません）")
        return 0
    ledger = _ledger(a)
    for it, how in write_plan(items, files, ledger):
        print(f"  {it.key}: 台帳 {how}")
    return 0


def _cmd_ledger(a: argparse.Namespace) -> int:
    states = _ledger(a).all_states()
    for key, st in states.items():
        m = st.manifest or {}
        part = f" 分割{m.get('chunk_index')}/{m.get('chunk_count')}" if (m.get("chunk_count") or 1) > 1 else ""
        print(f"{key} {st.state} 送信{st.attempts}回/メモ保存{st.memo_attempts}回 run={m.get('collector_run_id')} {m.get('direction')} "
              f"{m.get('memo')}{part} ツール実行ID={st.tool_run_id or '-'}")
    if not states:
        print("台帳は空です")
    return 0


def _need(ledger, key):
    st = ledger.state(key)
    if st.state == "NONE" or not st.manifest:
        print("[中止] 台帳に投入計画がありません")
        return None
    return st


def _cmd_baseline(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state != "PLANNED":
        print(f"[中止] 状態が{st.state}です（基準は送信前の計画段階でだけ記録します）")
        return 2
    ids = sorted(e.seq for e in parse_log_entries(open_text_auto(Path(a.log_html))))
    ledger.append(a.key, "BASELINE_RECORDED", before_run_ids=ids, manual=True)
    print(f"送信前の基準を記録しました（ログの実行ID {len(ids)}件、最大{max(ids) if ids else '-'}）。続けて統合ツールへ手動で投入してください")
    return 0


def _cmd_reconcile(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    m = st.manifest
    entries = parse_log_entries(open_text_auto(Path(a.log_html)))
    if st.state in ("MEMO_SAVED", "LINKED"):
        print(f"対応済みです: ツール実行ID={st.tool_run_id}")
        return 0
    if st.state == "RUN_IDENTIFIED":
        row = next((e for e in entries if e.seq == int(st.tool_run_id)), None)
        if row is not None and row.memo == m["memo"]:
            ledger.append(a.key, "MEMO_SAVED", tool_run_id=st.tool_run_id, via="log_html")
            print(f"メモ保存を確認しました: ツール実行ID={st.tool_run_id} メモ={row.memo!r}")
            return 0
        print(f"実行ID{st.tool_run_id}は確定済みですが、メモ{m['memo']!r}はまだ保存されていません。ログ画面で実行ID{st.tool_run_id}の行へ"
              "メモを保存してから、もう一度ログ画面を保存して実行してください（ASINは再投入しません）")
        return 3
    if st.state == "AMBIGUOUS":
        print("[停止] 対応を一意に確認できない状態です。resolveで人が実行IDを選んでください")
        return 3
    if not st.before_run_ids:
        print("[中止] 送信前の基準がありません。投入の前に baseline で送信前のログ画面を記録してください（過去の行と取り違えないため）")
        return 2
    d = identify_new_entry(entries, set(st.before_run_ids), expected_memo=m["memo"], exclude_run_ids=ledger.identified_run_ids(a.key),
                           response_run_id=a.response_run_id)
    if d.kind == "IDENTIFIED":
        ledger.append(a.key, "RUN_IDENTIFIED", tool_run_id=d.tool_run_id, observed_country=d.country, via="log_html")
        row = next(e for e in entries if e.seq == int(d.tool_run_id))
        print(f"実行IDを特定しました: {d.tool_run_id}（送信前の基準に無い新しいASINリスト行）。ログ画面でこの行へメモ{m['memo']!r}を保存してください")
        if row.memo == m["memo"]:
            ledger.append(a.key, "MEMO_SAVED", tool_run_id=d.tool_run_id, via="log_html")
            print("メモも保存済みです")
        return 0
    if d.kind == "AMBIGUOUS":
        ledger.append(a.key, "AMBIGUOUS", candidates=list(d.candidates))
        print(f"[曖昧] 新しい行が複数あり、送信結果との対応を一意に確認できません: {list(d.candidates)}。適当な行にメモを付けないでください")
        return 3
    print("基準に無い新しいASINリスト行が見つかりません（特定しません）")
    return 3


def _cmd_resolve(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    cands = [c for e in st.events if e["event"] == "AMBIGUOUS" for c in e.get("candidates", [])]
    if st.state != "AMBIGUOUS" or a.run_id not in cands:
        print(f"[中止] AMBIGUOUS状態の候補{sorted(set(cands))}の中から選んでください（状態={st.state}）")
        return 2
    ledger.append(a.key, "RUN_IDENTIFIED", tool_run_id=a.run_id, via="manual_choice")
    print(f"実行ID{a.run_id}を人の判断で採用しました。他の候補には何もしません")
    return 0


def _cmd_link_check(a: argparse.Namespace) -> int:
    from sqlalchemy import text

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state == "LINKED":
        print("対応記録済みです")
        return 0
    if st.state != "MEMO_SAVED":
        print(f"[中止] 状態が{st.state}です（メモ保存の確認後に、既存のingest-logで記録します）")
        return 2
    m = st.manifest
    with SessionFactory.for_app_role().get_session(m["business_market"]) as conn, conn.begin():
        row = conn.execute(text("SELECT seller_id, source FROM tool_execution_link WHERE marketplace = :m AND tool_run_id = :r"),
                           {"m": m["business_market"], "r": st.tool_run_id}).one_or_none()
    if row is None:
        print("tool_execution_linkにまだありません。保存したログ画面で `python -m tool_intake.cli ingest-log --file <html>` を実行してください")
        return 3
    if row.seller_id != m["source_key"]:
        print(f"[不一致] tool_execution_linkのseller_id={row.seller_id!r}が出典キー{m['source_key']!r}と違います。メモを確認してください")
        return 3
    ledger.append(a.key, "LINKED", tool_run_id=st.tool_run_id, seller_id=row.seller_id, source=row.source)
    print(f"対応記録済みを確認しました: {m['business_market']} run={st.tool_run_id} seller_id={row.seller_id}")
    return 0


def _cmd_verify_output(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    asins = [x for x in Path(st.manifest["input_file"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    r = verify_output(asins, Path(a.csv))
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 3


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _open_tool_page(selectors):
    """統合ツール専用のブラウザプロファイルで開き、利用者がログインしてEnterを押すのを待つ（認証情報は保存・入力しない）。"""
    from playwright.sync_api import sync_playwright

    from tool_submission.import_tool_page import ImportToolPage

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(Path("playwright_profiles") / "import_tool"), headless=False)
        try:
            page = context.new_page()
            page.goto(selectors.log_url)
            input("統合ツールにログインし、ログ画面が表示されたら、この画面でEnterを押してください...")
            yield ImportToolPage(page, selectors)
        finally:
            context.close()


def _cmd_submit(a: argparse.Namespace) -> int:
    from tool_submission.import_tool_page import Selectors
    from tool_submission.model import item_from_manifest
    from tool_submission.runner import submit_item

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    item = item_from_manifest(st.manifest)
    sel = Selectors(**json.loads(Path(a.selectors).read_text(encoding="utf-8"))) if a.selectors else Selectors()
    problems = None
    with _open_tool_page(sel) as page:
        problems = page.preflight()
        r = submit_item(ledger, page, item, Path(item.input_file))
    print(f"{r.kind}: 実行ID={r.tool_run_id or '-'} {r.detail or ''}（SAVEを押した={'はい' if r.clicked else 'いいえ'}）")
    return 0 if r.kind in ("MEMO_SAVED", "ALREADY_DONE") else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tool_submission.cli")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--ledger", default=None)
        sp.set_defaults(func=func)
        return sp

    sp = add("plan", _cmd_plan, "取得runから入力ファイルと投入計画を作る（DB読み取りのみ）")
    sp.add_argument("--fetch-market", required=True, choices=["US", "JP"], help="ASINを取得したAmazonの市場（collector_runの市場）")
    sp.add_argument("--run-id", required=True, type=int)
    sp.add_argument("--direction", default=None, choices=["EXPORT", "IMPORT"], help="出典の経路に方向が無い場合だけ明示する")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--dry-run", action="store_true")
    add("ledger", _cmd_ledger, "投入台帳の状態を表示する")
    sp = add("baseline", _cmd_baseline, "手動投入の前に、送信前のログ画面（保存HTML）から基準のID一覧を記録する")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-html", required=True)
    sp = add("reconcile", _cmd_reconcile, "送信後に保存したログ画面HTMLから、新しい行の実行IDを特定する（メモ保存の確認も）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-html", required=True)
    sp.add_argument("--response-run-id", type=int, default=None, help="送信直後の画面応答で実行IDが分かった場合")
    sp = add("resolve", _cmd_resolve, "新しい行が複数あるとき、人が実行IDを選ぶ")
    sp.add_argument("--key", required=True)
    sp.add_argument("--run-id", required=True, type=int)
    sp = add("submit", _cmd_submit, "統合ツールへ自動で送信しメモを保存する（入力画面のセレクタを--selectorsで渡すまで実行不可。実画面は未検証）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--selectors", default=None, help="入力画面のセレクタ等のJSON（Selectorsの項目名）。利用者がDOMを確認した値だけを入れる")
    sp = add("link-check", _cmd_link_check, "ingest-log後に、tool_execution_linkへの記録（対応記録済み）を確認する（DB読み取りのみ）")
    sp.add_argument("--key", required=True)
    sp = add("verify-output", _cmd_verify_output, "出力CSVのASINが入力に含まれるか、事後に確認する（送信結果と実行IDの対応の確認）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--csv", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
