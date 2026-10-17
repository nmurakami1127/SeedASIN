"""統合ツール投入の計画・台帳・照合のCLI。

    python -m tool_submission.cli plan --fetch-market US --run-id 82 [--dry-run]
    python -m tool_submission.cli ledger
    # 手動で投入する場合（画面の手順は docs/DEVIATIONS.md #60）
    python -m tool_submission.cli begin     --key <投入キー> --log-json <送信直前のログ一覧> --options-json <画面の設定値>   # 送信権の確保・送信開始の記録（SAVEの前）
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
from tool_submission.reconcile import corroborate, identify_new_entry, load_log_entries, single_row_html, verify_output

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
    ids = sorted(e.seq for e in load_log_entries(a.log_html))
    ledger.append(a.key, "BASELINE_RECORDED", before_run_ids=ids, manual=True)
    print(f"送信前の基準を記録しました（ログの実行ID {len(ids)}件、最大{max(ids) if ids else '-'}）。続けて統合ツールへ手動で投入してください")
    return 0


def _cmd_begin(a: argparse.Namespace) -> int:
    """送信権の確保と送信開始の記録（自動・手動・外部ブラウザ操作の全てで、ASINの入力・SAVEの前に必須）。"""
    from tool_submission.model import item_from_manifest
    from tool_submission.runner import claim_submission

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    item = item_from_manifest(st.manifest)
    ids = [e.seq for e in load_log_entries(a.log_json)]
    options = json.loads(Path(a.options_json).read_text(encoding="utf-8")) if a.options_json else None
    ok, msg = claim_submission(ledger, item, Path(item.input_file), ids, options)
    print(("[確保] " if ok else "[中止] ") + msg)
    if ok:
        print(f"対象: 取得run={item.collector_run_id} 業務方向={item.direction} ASIN{item.asin_count}件 sha256={item.input_sha256[:16]}… メモ（後付け）={item.memo!r}")
    return 0 if ok else 2


def _cmd_evidence(a: argparse.Namespace) -> int:
    """SAVE直後に画面から得た「送信リクエストと応答」の記録を検証して台帳に残す（実行IDの対応根拠）。
    capture: {"endpoint": "...start_search4.php", "body": "<送信したJSON文字列>", "status": 200, "response": "<応答テキスト>"}"""
    import hashlib

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state != "SUBMITTING":
        print(f"[中止] 状態が{st.state}です（evidenceは送信開始の記録後・実行ID特定の前に残します）")
        return 2
    cap = json.loads(Path(a.capture_json).read_text(encoding="utf-8"))
    m = st.manifest
    plan_asins = [x for x in Path(m["input_file"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    problems = []
    try:
        body = json.loads(cap["body"])
    except (KeyError, ValueError):
        body = {}
        problems.append("送信リクエストの本文を読めません")
    sent = [x for x in str(body.get("url", "")).split(",") if x]
    if sent != plan_asins:
        problems.append(f"送信したASIN（{len(sent)}件）が計画（{len(plan_asins)}件）と一致しません")
    if not str(cap.get("endpoint", "")).endswith("start_search4.php"):
        problems.append("送信先が想定（start_search4.php）と違います")
    if body.get("type") != "asin_list" or body.get("country") not in ("", None):
        problems.append(f"種別/国指定が想定と違います（type={body.get('type')!r}, country={body.get('country')!r}）")
    resp = str(cap.get("response", ""))
    # 実画面の応答（2026-10-17確認）: 成功時は "start_search4asin_listrivalsuccess asin<最古ID> https://…/csv_del.php<br>" のように"success"を含む。
    # 失敗は session_expired / no_api / no_f_api / url_au / url_ca / ban / no_me / not_release / not_http（画面のスクリプトの分岐）。"end"は別の分岐。
    failure_tokens = ("session_expired", "no_api", "no_f_api", "url_au", "url_ca", "ban", "no_me", "not_release", "not_http")
    hit = [t for t in failure_tokens if t in resp]
    if cap.get("status") != 200 or hit or not ("success" in resp or "end" in resp):
        problems.append(f"応答が保存成功ではありません（status={cap.get('status')}, 失敗の目印={hit}, 応答={resp[:80]!r}）")
    if (body.get("options") or {}).get("variation"):
        problems.append("送信リクエストのバリエーションがONです")
    ev = {"ok": not problems, "problems": problems, "asin_count": len(sent), "asin_list_sha256": hashlib.sha256(",".join(sent).encode()).hexdigest(),
          "request_options": body.get("options"), "response": str(cap.get("response", ""))[:120], "alerts": cap.get("alerts", [])}
    ledger.append(a.key, "EVIDENCE", evidence=ev)
    print(("根拠を記録しました: " if ev["ok"] else "[不一致] 根拠として認められません: ")
          + json.dumps({k: ev[k] for k in ("ok", "problems", "asin_count", "response")}, ensure_ascii=False))
    return 0 if ev["ok"] else 3


def _cmd_memo_start(a: argparse.Namespace) -> int:
    """メモ保存ボタンを押す前に、台帳へ「メモ保存開始」を記録する（実行ID確定後。ASINは再投入しない）。"""
    from tool_submission.ledger import MEMO_MAX_ATTEMPTS

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state != "RUN_IDENTIFIED" or not st.tool_run_id:
        print(f"[中止] 状態が{st.state}です（実行ID確定・メモ未保存の計画だけがメモを保存できます）")
        return 2
    if st.memo_attempts >= MEMO_MAX_ATTEMPTS:
        print(f"[中止] メモ保存の試行が上限（{MEMO_MAX_ATTEMPTS}回）に達しています。画面で確認してください")
        return 2
    ledger.append(a.key, "MEMO_SAVE_STARTED", tool_run_id=st.tool_run_id)
    print(f"メモ保存を開始します: 実行ID={st.tool_run_id} メモ={st.manifest['memo']!r}（この実行IDの行だけ。ASINは再投入しません）")
    return 0


def _cmd_mark_not_submitted(a: argparse.Namespace) -> int:
    """送信開始を記録したが、SAVEを押していない（または保存されなかった）と、**ログ画面で確認できた**ときだけ、未送信に戻す。"""
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state != "SUBMITTING":
        print(f"[中止] 状態が{st.state}です（結果不明・特定済みの計画は未送信へ戻しません）")
        return 2
    new = [e.seq for e in load_log_entries(a.log_json) if e.seq not in set(st.before_run_ids)]
    if new:
        print(f"[中止] 送信前の基準に無い新規行{new}があります。未送信とは確認できません（結果不明として照合してください）")
        return 3
    ledger.append(a.key, "NOT_SUBMITTED_VERIFIED", reason=a.reason, via="log_snapshot")
    print("未送信を確認して記録しました（基準に無い新規行なし）")
    return 0


def _cmd_ingest(a: argparse.Namespace) -> int:
    """既存のingest-logを、この投入の実行IDの1行だけに限定して実行する（DB更新: seller登録・tool_execution_link・取得完了なら出力取込）。"""
    import tempfile

    from tool_intake.log_ingest import ingest_log

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.state not in ("MEMO_SAVED", "LINKED") or not st.tool_run_id:
        print(f"[中止] 状態が{st.state}です（メモ保存の確認後に取り込みます）")
        return 2
    try:
        html = single_row_html(Path(a.row_html).read_text(encoding="utf-8"), int(st.tool_run_id), st.manifest["memo"])
    except ValueError as exc:
        print(f"[中止] {exc}")
        return 2
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "one_row.html"
        p.write_text(html, encoding="utf-8")
        factory = SessionFactory.for_app_role()
        summary = ingest_log(factory, p, Path(a.output_dir)) if a.output_dir else ingest_log(factory, p)
    print(summary.format())
    return 0 if summary.other_errors == 0 and summary.parse_errors == 0 else 3


def _cmd_reconcile(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    m = st.manifest
    entries = load_log_entries(a.log_html)
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
        row = next(e for e in entries if e.seq == int(d.tool_run_id))
        if not a.no_evidence:
            problems = corroborate(row, st.evidence)
            if problems:
                ledger.append(a.key, "OUTCOME_UNKNOWN", reason=("対応根拠を確認できない: " + "; ".join(problems))[:300], candidate=d.tool_run_id)
                print(f"[停止] 新規行{d.tool_run_id}がありますが、自分の送信と対応する根拠を確認できません"
                      f"（結果不明として止めます。メモを付けないでください）: {problems}")
                return 3
        ledger.append(a.key, "RUN_IDENTIFIED", tool_run_id=d.tool_run_id, observed_country=d.country, via="log_html")
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
    sp = add("begin", _cmd_begin, "送信権を確保して送信開始を記録する（ASINの入力・SAVEの前に必須。自動・手動・外部ブラウザ操作の全て）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-json", required=True, help="送信直前のログ一覧のスナップショット（JSONまたは保存HTML）")
    sp.add_argument("--options-json", default=None, help="画面の設定値（表示名→ON/OFFのJSON。バリエーションを含むこと）")
    sp = add("ingest", _cmd_ingest, "既存のingest-logを、この投入の実行IDの1行だけに限定して実行する（メモ保存の確認後）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--row-html", required=True, help="その実行IDの行（またはログ画面）のHTML")
    sp.add_argument("--output-dir", default=None, help="統合ツールの出力CSVのフォルダー（既定は既存のingest-logと同じ）")
    sp = add("reconcile", _cmd_reconcile, "送信後に保存したログ画面HTMLから、新しい行の実行IDを特定する（メモ保存の確認も）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-html", required=True)
    sp.add_argument("--response-run-id", type=int, default=None, help="送信直後の画面応答で実行IDが分かった場合")
    sp.add_argument("--no-evidence", action="store_true", help="（非推奨）送信リクエスト・応答の根拠なしで特定する。既定では根拠が必要")
    sp = add("evidence", _cmd_evidence, "SAVE直後に得た送信リクエストと応答を検証して台帳に残す（実行IDの対応根拠）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--capture-json", required=True)
    sp = add("memo-start", _cmd_memo_start, "メモ保存ボタンを押す前に、台帳へメモ保存開始を記録する（実行ID確定後）")
    sp.add_argument("--key", required=True)
    sp = add("mark-not-submitted", _cmd_mark_not_submitted, "SAVEを押していない/保存されなかったと、ログで確認できたときだけ未送信に戻す")
    sp.add_argument("--key", required=True)
    sp.add_argument("--log-json", required=True)
    sp.add_argument("--reason", required=True)
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
