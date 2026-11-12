"""統合ツール投入の計画・台帳・照合のCLI。

    python -m tool_submission.cli plan --fetch-market US --run-id 82 [--dry-run]
    python -m tool_submission.cli ledger
    python -m tool_submission.cli plan-pool [--apply] [--report-dir DIR] [--market US|JP|both] [--direction EXPORT|IMPORT] [--source ID]   # 保存済みの未登録ASINを出典×方向ごとに5,000件以下で計画（既定は読み取りのみ）
    python -m tool_submission.cli pool-history-check [--market ...]   # plan-poolの登録対象が統合ツールの履歴(tool_output_asin等)に既にあるか（読み取り専用）
    python -m tool_submission.cli audit-coverage [--market US|JP|both] [--only-gaps] [--min-asins 5001] [--detail] [--json]   # 取得ASINの投入計画への反映を確認（読み取り専用）
    # 手動で投入する場合（画面の手順は docs/DEVIATIONS.md #60）
    python -m tool_submission.cli begin     --key <投入キー> --log-json <送信直前のログ一覧> --options-json <画面の設定値>   # 送信権の確保・送信開始の記録（SAVEの前）
    python -m tool_submission.cli reconcile --key <投入キー> --log-html <送信後に保存したログ画面.html>
    python -m tool_submission.cli resolve   --key <投入キー> --run-id <実行ID>        # 新規行が複数のとき、人が選ぶ
    python -m tool_submission.cli link-check --key <投入キー>                        # ingest-log後、対応記録済みを確認（DB読み取りのみ）
    python -m tool_submission.cli verify-output --key <投入キー> --csv <統合ツールの出力CSV>
    python -m tool_submission.cli withdraw-duplicate --key <投入キー> --evidence-run <統合ツール実行ID> --reason "…" [--verify-csv <出力CSV>]   # 重複のため未送信の計画を取り下げる（追記のみ・y/n確認）
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
from tool_submission.ledger import SubmissionLedger, max_attempts
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
    entries = load_log_entries(a.log_json)
    ids = [e.seq for e in entries]
    options = json.loads(Path(a.options_json).read_text(encoding="utf-8")) if a.options_json else None
    ok, msg = claim_submission(ledger, item, Path(item.input_file), ids, options, log_entries=entries,
                               db_rows=None if a.no_db else _db_rows(), retention_log=DEFAULT_DIR / "log_retention.jsonl")
    print(("[確保] " if ok else "[中止] ") + msg)
    if ok:
        print(f"対象: 取得run={item.collector_run_id} 業務方向={item.direction} ASIN{item.asin_count}件 sha256={item.input_sha256[:16]}… メモ（後付け）={item.memo!r}")
    return 0 if ok else 2


def _db_rows():
    """ログの行ごとに、tool_output_asinへ取り込み済みの件数（DB読み取りのみ）。ASINと取得日時だけで、元CSVの全列の保全ではない。"""
    from sqlalchemy import text

    factory = SessionFactory.for_app_role()

    def count(market: str, seq: int) -> int:
        with factory.get_session(market) as conn:
            return int(conn.execute(text("SELECT count(*) FROM tool_output_asin WHERE marketplace=:m AND tool_run_id=:r"),
                                    {"m": market, "r": str(seq)}).scalar_one())
    return count


def _cmd_retention_check(a: argparse.Namespace) -> int:
    """新しい送信で削除される最古のログ行（と出力CSV）が保全されているかを送信前に確認する（読み取りのみ。記録はローカルJSONL）。"""
    from tool_submission.retention import LOG_LIMIT, blockers_of, check_retention, record_retention

    entries = load_log_entries(a.log_json)
    items = check_retention(entries, will_add=a.will_add, db_rows=None if a.no_db else _db_rows())
    print(f"ログ{len(entries)}行（上限{LOG_LIMIT}）。今回{a.will_add}件追加で削除される可能性のある行: {len(items)}")
    for it in items:
        flag = "OK " if it.ok else "停止"
        print(f"  [{flag}] ログ{it.seq} 状態={it.status} メモ={it.memo or '-'} CSV={it.csv or '-'} 市場={it.market or '-'} "
              f"ローカル元CSV={'あり ' + it.local_sha256[:12] if it.local_files else 'なし'} DB取込={'-' if it.db_rows is None else it.db_rows}件")
        for w in it.warnings:
            print(f"        注意: {w}")
        for b in it.blockers:
            print(f"        停止理由: {b}")
    if not a.no_record:
        n = record_retention(DEFAULT_DIR / "log_retention.jsonl", items, context={"will_add": a.will_add})
        print(f"[記録] {n}行をlog_retention.jsonlへ追記しました")
    held = blockers_of(items)
    print("[結果] " + ("新規送信を止めます" if held else "削除対象の保全を確認できました"))
    return 3 if held else 0


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
    from tool_submission.evidence import check_capture

    m = st.manifest
    plan_asins = [x for x in Path(m["input_file"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = check_capture(plan_asins, cap)
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


def _output_target(a):
    """（台帳、状態、入力ASIN、市場、実行ID）。対応済み（メモ保存済み以降）でなければ出力を扱わない。"""
    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return None
    if st.state not in ("MEMO_SAVED", "LINKED") or not st.tool_run_id:
        print(f"[中止] 状態が{st.state}です（実行IDとメモの対応が確定してから出力を扱います）")
        return None
    asins = [x for x in Path(st.manifest["input_file"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    return ledger, st, asins, st.manifest["business_market"], st.tool_run_id


def _cmd_find_output(a: argparse.Namespace) -> int:
    """既存のautomation.pyが保存済みの出力CSVがローカルにあるか探す（取得・ダウンロードはしない）。"""
    from tool_intake.processed_file_lookup import find_tool_output_files
    from tool_submission.retention import LOCAL_ORIGINAL_DIRS

    t = _output_target(a)
    if t is None:
        return 2
    _, st, asins, market, run_id = t
    processed = find_tool_output_files(market, run_id)
    pending = [p for d in LOCAL_ORIGINAL_DIRS if "incoming" in str(d) and d.is_dir() for p in d.iterdir() if p.name.startswith(f"{run_id}_")]
    print(f"ツール実行ID={run_id} 市場={market} 入力ASIN={len(asins)}件")
    print(f"  処理済みフォルダー: {[str(p) for p in processed] or 'なし'}")
    print(f"  取り込み待ちフォルダー: {[str(p) for p in pending] or 'なし'}")
    return 0 if processed else 3


def _cmd_import_output(a: argparse.Namespace) -> int:
    """検証済みの出力だけを既存の取込（IntegrationToolImportService）でDBへ入れる。入力に無いASINがあれば取り込まない。"""
    from tool_intake.integration_tool_import import IntegrationToolImportService
    from tool_intake.processed_file_lookup import find_tool_output_files

    t = _output_target(a)
    if t is None:
        return 2
    ledger, st, asins, market, run_id = t
    if a.csv:
        path = Path(a.csv)
    else:
        files = find_tool_output_files(market, run_id)
        if len(files) != 1:
            print(f"[中止] 処理済みフォルダーの出力CSVが{len(files)}件です（1件に特定できません。--csvで指定してください）")
            return 3
        path = files[0]
    if not path.name.startswith(f"{run_id}_"):
        print(f"[中止] ファイル名が実行ID{run_id}で始まりません（取り違えの疑い）: {path.name}")
        return 3
    r = verify_output(asins, path)
    print(json.dumps(r, ensure_ascii=False))
    if not r.get("ok"):
        print("[中止] 出力に入力へ含まれないASINがある、または空/ASIN列なしのため取り込みません（実行IDの取り違えの疑い）")
        return 3
    factory = SessionFactory.for_app_role()
    count = _db_rows()
    before = count(market, int(run_id))
    svc = IntegrationToolImportService()
    s1 = svc.import_file(factory, market, run_id, path)
    mid = count(market, int(run_id))
    print(f"取込: 行{s1.total_rows} 成功{s1.success_rows} エラー{s1.error_rows} / DB件数 {before} -> {mid}")
    ledger.append(a.key, "OUTPUT_IMPORTED", tool_run_id=run_id, file=path.name, before=before, after=mid,
                  output_rows=r["output_rows"], output=r["output"], missing_from_output=r["missing_from_output"])
    if a.recheck:
        s2 = svc.import_file(factory, market, run_id, path)
        after = count(market, int(run_id))
        print(f"再実行: 成功{s2.success_rows} / DB件数 {mid} -> {after}（{'重複なし' if after == mid else '件数が変わりました！'}）")
        return 0 if after == mid else 3
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
def _open_tool_page(selectors, *, wait_for_login=True, playwright=None):
    """統合ツール専用のブラウザプロファイルで開き、利用者がログインしてEnterを押すのを待つ（認証情報は保存・入力しない）。"""
    from playwright.sync_api import sync_playwright

    from tool_submission.import_tool_page import ImportToolPage

    if playwright is None:
        with sync_playwright() as runtime:
            with _open_tool_page(selectors, wait_for_login=wait_for_login, playwright=runtime) as page:
                yield page
        return
    context = playwright.chromium.launch_persistent_context(str(Path("playwright_profiles") / "import_tool"), headless=False)
    try:
        page = context.new_page()
        page.add_init_script("document.addEventListener('DOMContentLoaded', () => { document.title = '【SeedASIN 自動投入用】' + document.title; });")
        page.goto(selectors.log_url)
        page.bring_to_front()
        if wait_for_login:
            _wait_for_tool_login(page, selectors)
        yield ImportToolPage(page, selectors)
    finally:
        context.close()


def _wait_for_tool_login(page, selectors, *, timeout=300, sleep=None, monotonic=None):
    """ログイン済みなら入力待ちを挟まない。未ログイン時の認証は人だけが行う。"""
    import time

    from tool_submission.import_tool_page import LOG_HEADER_MARKERS

    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    deadline = monotonic() + timeout
    announced = False
    while True:
        header = page.locator("table tr").first
        if header.count() and all(marker in header.inner_text() for marker in LOG_HEADER_MARKERS):
            return
        if "/asin/" in page.url and "login" not in page.url.lower() and page.locator('input[type="password"]').count() == 0 and page.url != selectors.log_url:
            page.goto(selectors.log_url)
            continue
        if not announced:
            print("【手動対応が必要】開いた統合ツールのブラウザでログインしてください。完了後に自動で続行します。", flush=True)
            announced = True
        if monotonic() >= deadline:
            raise EOFError("統合ツールのログイン待ちが時間切れになりました")
        sleep(3)
        # ログイン入力中の画面は再読込しない。認証後、メイン画面に移った時だけログへ進む。
        if "/asin/" in page.url and "login" not in page.url.lower() and page.locator('input[type="password"]').count() == 0:
            page.goto(selectors.log_url)


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


def _cmd_abandon(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    try:
        ledger.abandon_unknown(a.key, reason=a.reason)
    except ValueError as exc:
        print(f"[中止] {exc}")
        return 2
    print("結果不明の投入の追跡を取り下げました。再送・メモ保存はしません。統合ツール上のジョブは取消していません。")
    return 0


def _default_memo(st, run_id: str, missing: int | None, today: str) -> str:
    m = st.manifest or {}
    check = f"照合日{today}・不足{missing}件" if missing is not None else f"照合日{today}・出力CSVとの照合なし"
    return (f"重複のため取り下げ（未送信）。この鍵の入力ASINは、統合ツール実行 {run_id} の結果に全件含まれる（{check}）。"
            f"同一ASINの再投入を避けるため送信しない。履歴・入力ファイルは削除しない。"
            f"（方向={m.get('direction')}・出典={m.get('source_key') or m.get('source_id')}）")


def _cmd_withdraw_duplicate(a: argparse.Namespace, confirm=None) -> int:
    """重複のため、未送信(PLANNED・試行0)の計画を取り下げる。台帳へ追記するだけ（削除・書換えなし）。実行前に内容を表示してy/n確認する。"""
    import hashlib
    from datetime import datetime, timezone

    ledger = _ledger(a)
    st = _need(ledger, a.key)
    if st is None:
        return 2
    if st.duplicate_withdrawn:
        print(f"既に重複のため取り下げ済みです（実行 {st.duplicate_withdrawn.get('evidence_run_id')}・{st.duplicate_withdrawn.get('ts')}）。何も追記しません")
        return 0
    if st.state != "PLANNED" or st.attempts != 0:
        print(f"[拒否] 状態が{st.state}（送信の試行{st.attempts}回）です。未送信のPLANNED（試行0回）だけを取り下げられます")
        return 2
    m = st.manifest or {}
    path = Path(m.get("input_file", "").replace("\\", "/"))
    try:
        asins = [x.strip() for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        asins = []
    missing, ev_file = None, a.evidence_file
    if a.verify_csv:
        if not asins:
            print(f"[拒否] 入力ファイルを読めません: {m.get('input_file')}（照合できないため取り下げません）")
            return 3
        r = verify_output(asins, Path(a.verify_csv))
        if "reason" in r:
            print(f"[拒否] 出力CSVを照合できません: {r['reason']}")
            return 3
        missing = int(r.get("missing_from_output", len(asins)))
        if missing > 0 or not r.get("output"):
            print(f"[拒否] この鍵の入力ASIN {r.get('input')}件のうち、出力CSV（{r.get('output')}件）に無いものが{missing}件あります。取り下げません"
                  f"（例: {', '.join(r.get('missing_asins', [])[:5])}）")
            return 3
        ev_file = ev_file or a.verify_csv
    sha = None
    if ev_file:
        try:
            sha = hashlib.sha256(Path(ev_file).read_bytes()).hexdigest()
        except OSError:
            print(f"[拒否] 根拠ファイルを読めません: {ev_file}")
            return 3
    # 方向・出典の一致は表示のみ（未確認でも進められるが警告する）
    warn = []
    same_run = [(k, s2) for k, s2 in ledger.all_states().items() if s2.tool_run_id == str(a.evidence_run) and k != a.key and s2.manifest]
    if same_run:
        om = same_run[0][1].manifest
        for label, mine, theirs in (("方向", m.get("direction"), om.get("direction")), ("市場", m.get("business_market"), om.get("business_market")),
                                    ("出典", m.get("source_key") or m.get("source_id"), om.get("source_key") or om.get("source_id"))):
            if mine != theirs:
                warn.append(f"{label}が一致しません（この鍵={mine} / 実行{a.evidence_run}の鍵={theirs}）")
    else:
        warn.append(f"実行{a.evidence_run}に対応する台帳の鍵が見つからず、方向・出典の一致を確認できません")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    memo = _default_memo(st, str(a.evidence_run), missing, today) + (f" 備考: {a.note}" if a.note else "")
    print("== 重複のため取り下げ（台帳に追記するだけ。削除・書換えはしません） ==")
    print(f"対象: {a.key}  状態={st.state}  方向={m.get('direction')}  出典={m.get('source_key') or m.get('source_id')}  ASIN={m.get('asin_count')}件  run={m.get('collector_run_id')}")
    print(f"根拠: 統合ツール実行 {a.evidence_run}" + (f"  照合CSV={a.verify_csv}（不足{missing}件）" if a.verify_csv else "  （出力CSVとの照合なし）") + (f"  根拠ファイルsha256={sha[:16]}…" if sha else ""))
    print(f"理由: {a.reason}")
    print(f"メモ: {memo}")
    for w in warn:
        print(f"【警告】{w}")
    if confirm is None:
        import sys
        if not sys.stdin.isatty():
            print("[中止] 対話端末でのy/n確認が必要です")
            return 2
        confirm = lambda q: input(q).strip().lower() == "y"  # noqa: E731
    if not confirm("この内容で取り下げます。よろしいですか？ [y/n] "):
        print("取り下げませんでした（何も追記していません）")
        return 2
    try:
        _st, added = ledger.withdraw_duplicate(a.key, evidence_run_id=str(a.evidence_run), reason=a.reason, memo=memo, evidence_sha=sha,
                                               evidence_file=str(ev_file) if ev_file else None, verified=bool(a.verify_csv), missing_count=missing,
                                               overlap_count=(len(set(x.upper() for x in asins)) - missing) if missing is not None else None,
                                               note=a.note, warnings=warn, direction=m.get("direction"), source_key=m.get("source_key"))
    except ValueError as exc:
        print(f"[拒否] {exc}")
        return 2
    print("取り下げを台帳に追記しました。" if added else "既に取り下げ済みでした（何も追記していません）。", "この鍵は再送・再計画されません。履歴と入力ファイルは残しています。")
    return 0


def _cmd_grant_retry(a: argparse.Namespace) -> int:
    ledger = _ledger(a)
    try:
        st = ledger.grant_retry(a.key, reason=a.reason, reopen_abandoned=a.reopen_abandoned)
    except ValueError as exc:
        print(f"[中止] {exc}")
        return 1
    print(f"再試行を1回付与しました: {a.key} 状態={st.state} 試行={st.attempts}/{max_attempts(st)}")
    return 0


def _cmd_audit_coverage(a: argparse.Namespace) -> int:
    """取得ASINと投入計画の対応を確認する（読み取り専用。DB・台帳・入力ファイルに書かない。再計画もしない）。"""
    from tool_submission import coverage_audit

    markets = ["US", "JP"] if a.market == "both" else [a.market]
    rep = coverage_audit.audit(SessionFactory.for_app_role(), _ledger(a), markets, batch_target=a.batch_target, flush=a.flush,
                               skip_failures=a.skip_failures, base_dirs=[Path(x) for x in (a.input_base or [])])
    states = tuple(x.strip().upper() for x in a.state.split(",")) if a.state else ()
    if a.json:
        rep = {**rep, "rows": [r for r in rep["rows"] if r["total"] >= a.min_asins and (not states or (r["cycle_state"] or "LEGACY") in states)]}
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=list))
    else:
        print("\n".join(coverage_audit.format_report(rep, only_gaps=a.only_gaps, min_asins=a.min_asins, detail=a.detail, states=states)))
    return 0 if rep["identity_ok"] else 1


def _pool_args(a, factory):
    """plan-pool / pool-history-checkの共通: 設定・台帳・読み取り専用の接続・計算結果。"""
    from datetime import datetime, timezone

    from tool_submission import pool
    from workflow.config import load_config

    cfg = load_config(a.config)
    ledger = SubmissionLedger(Path(a.ledger) if a.ledger else Path(cfg.submission_dir) / "ledger.jsonl")
    if factory is None:
        from workflow.dbconn import readonly_factory, resolve_app_url

        print(f"[接続] app_role（読み取り専用）: {resolve_app_url()[1]}")
        factory = readonly_factory()
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    markets = ["US", "JP"] if a.market == "both" else [a.market]
    rep = pool.compute(factory, cfg, ledger, now=now, out_dir=Path(a.out_dir) if a.out_dir else None, markets=markets,
                       directions={a.direction} if a.direction else None, sources=set(a.source) if a.source else None,
                       base_dirs=[Path(x) for x in (a.input_base or [])])
    return pool, cfg, ledger, factory, rep


def _cmd_plan_pool(a: argparse.Namespace, factory=None, confirm=None) -> int:
    """保存済みの未登録ASINを出典×方向ごとに5,000件以下で計画する。既定は読み取りのみ。--applyだけ、y/n確認の上で台帳へPLANNEDを追記する（送信はしない）。"""
    import sys

    pool, cfg, ledger, factory, rep = _pool_args(a, factory)
    print("\n".join(pool.format_report(rep)))
    if a.report_dir:
        print(f"レポート: {pool.write_report(rep, Path(a.report_dir))}")
    code = 0 if rep["identity_ok"] and not rep["errors"] else 1
    if not a.apply:
        print("（読み取りのみです。何も書いていません。台帳へ追記するなら --apply）")
        return code
    n = len(rep["items"])
    if n == 0:
        print("追記する新しい入力はありません")
        return code
    if confirm is None:
        if not sys.stdin.isatty():
            print("[中止] --apply は対話端末でのy/n確認が必要です")
            return 2
        confirm = lambda q: input(q).strip().lower() == "y"  # noqa: E731
    if not confirm(f"台帳へ新しい入力{n}件（{sum(e['item'].asin_count for e in rep['items']):,} ASIN）をPLANNEDとして追記します（送信はしません）。よろしいですか [y/N] "):
        print("確認が取れなかったので何も変更していません")
        return 2
    from workflow.store import WorkflowBusy

    try:  # 計画の確定はロックを取り、台帳を読み直してから追記する（watch・登録ループとの二重計画を防ぐ）
        res = pool.plan_pool(factory, cfg, ledger, now=rep["now"], out_dir=Path(a.out_dir) if a.out_dir else None, markets=rep["params"]["markets"],
                             directions=set(rep["params"]["directions"] or []) or None, sources=set(rep["params"]["sources"] or []) or None,
                             base_dirs=[Path(x) for x in (a.input_base or [])])
    except WorkflowBusy as exc:
        print(f"[中止] 他の計画処理（watch・登録ループ・別のplan-pool --apply）が実行中のため、何も書いていません: {exc}")
        return 2
    rep, done, skipped = res["report"], res["written"], res["skipped"]
    print(f"台帳へ追記しました: 入力{len(done)}件・{sum(e['item'].asin_count for e in done):,} ASIN（送信はしていません）")
    for s_ in skipped:
        print(f"  書かなかった: {s_['key']}: {s_['reason']}")
    if a.report_dir:
        print(f"レポート: {pool.write_report(rep, Path(a.report_dir), applied=True, extra={'written': [e['item'].key for e in done], 'skipped': skipped})}")
    return code if not skipped else 1


def _cmd_pool_history_check(a: argparse.Namespace, factory=None) -> int:
    """plan-poolの登録対象が、統合ツールの履歴（tool_output_asin・tool_execution_link）に既にあるかを数える（DBはSELECTのみ。台帳・入力ファイルには書かない）。"""
    pool, cfg, ledger, factory, rep = _pool_args(a, factory)
    rows = pool.history_check(factory, rep)
    print("pool-history-check: 登録対象ASINと統合ツール履歴の重なり（読み取り専用。登録経路には使いません）")
    for r in rows:
        print(f"  {r['market']} 出典{r['source_id']}({str(r['source_key'])[:24]}) {r['direction']}（業務市場{r['business_market']}）: 履歴に同じ出典・方向で既にあり除外{r['excluded_same_source']:,}"
              f" ／残った登録対象{r['targets']:,} ／うち他の出典の実行の出力にある{r['in_output_any_source']:,}（除外しない）"
              f" ／この出典キーの対応記録（実行）{r['source_link_count']}件")
    if not rows:
        print("  登録対象なし")
    else:
        print(f"  合計: 履歴で除外{sum(r['excluded_same_source'] for r in rows):,} ／残った登録対象{sum(r['targets'] for r in rows):,} ／うち他の出典の履歴にもある{sum(r['in_output_any_source'] for r in rows):,}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tool_submission.cli")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--ledger", default=None)
        sp.set_defaults(func=func)
        return sp

    sp = add("audit-coverage", _cmd_audit_coverage, "取得ASINが投入計画に漏れなく入っているか確認する（読み取り専用。再計画はしない）")
    sp.add_argument("--market", default="both", choices=["US", "JP", "both"], help="取得先市場（collector_runの市場）")
    sp.add_argument("--only-gaps", action="store_true", help="未計画・取り下げ済みのある周期だけ表示する")
    sp.add_argument("--min-asins", type=int, default=0, help="周期のASIN数がこの件数以上の周期だけ表示する（例: 5001で5,000件超のみ）")
    sp.add_argument("--detail", action="store_true", help="分割の親（取得run・分割親id）ごとの入力数・ASIN数・状態を表示する")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--state", default=None, help="周期の状態で絞る（カンマ区切り。例: OPEN,BROKEN。旧方式はLEGACY）")
    sp.add_argument("--batch-target", type=int, default=5000, help="ドライラン計算の前提（本番設定のbatch_target_asins。既定5000）")
    sp.add_argument("--flush", action="store_true", help="（前提の記録用）flush有効・猶予経過を仮定した場合の数は would_plan_flush に常に出る")
    sp.add_argument("--skip-failures", action="store_true", help="ドライラン計算の前提: 本番設定のskip_failures=true（失敗runの保存分も入力にする）")
    sp.add_argument("--input-base", action="append", default=None, help="入力ファイルの相対パスを探す基準フォルダー（複数可。既定は台帳のフォルダーとその親・カレント）")

    for name, fn, help_ in (("plan-pool", _cmd_plan_pool, "保存済みの未登録ASINを出典×方向ごとに5,000件以下で投入計画にする（既定は読み取りのみ。--applyで台帳へ追記）"),
                            ("pool-history-check", _cmd_pool_history_check, "plan-poolの登録対象が統合ツールの履歴に既にあるか確認する（読み取り専用）")):
        sp = add(name, fn, help_)
        sp.add_argument("--config", default="config/workflow.json")
        sp.add_argument("--market", default="both", choices=["US", "JP", "both"], help="取得先市場（collector_runの市場）")
        sp.add_argument("--direction", default=None, choices=["EXPORT", "IMPORT"])
        sp.add_argument("--source", type=int, action="append", default=None, help="出典ID（複数可）")
        sp.add_argument("--now", default=None, help="基準時刻（ISO形式。テスト・検証用。既定は現在）")
        sp.add_argument("--out-dir", default=None, help="入力ファイルの出力先（既定は設定のsubmission_dir）")
        sp.add_argument("--input-base", action="append", default=None, help="台帳の入力ファイルの相対パスを探す基準フォルダー（複数可）")
        if name == "plan-pool":
            sp.add_argument("--apply", action="store_true", help="y/n確認の上で台帳へPLANNEDを追記する（送信はしない）")
            sp.add_argument("--report-dir", default=None, help="レポート（JSONとテキスト）の出力先")

    sp = add("abandon", _cmd_abandon, "結果不明の投入の追跡を取り下げる（外部ジョブの取消ではない）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--reason", required=True)

    sp = add("grant-retry", _cmd_grant_retry, "未送信と確認済みの投入に再試行を1回分追加する（台帳へ追記のみ）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--reopen-abandoned", action="store_true", help="取り下げ済み(ABANDONED)を未送信に戻して付与する")

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
    sp.add_argument("--no-db", action="store_true", help="削除対象のDB取込の有無を調べない")
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
    sp = add("withdraw-duplicate", _cmd_withdraw_duplicate, "重複のため未送信(PLANNED・試行0)の計画を取り下げる（台帳へ追記のみ・y/n確認）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--evidence-run", required=True, help="同じASINを含む統合ツールの実行ID")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--evidence-file", default=None, help="根拠のファイル（sha256を記録する）")
    sp.add_argument("--verify-csv", default=None, help="統合ツール出力CSV: 入力ASINが全て含まれる場合のみ許可（不足があれば拒否）")
    sp.add_argument("--note", default=None)

    sp = add("link-check", _cmd_link_check, "ingest-log後に、tool_execution_linkへの記録（対応記録済み）を確認する（DB読み取りのみ）")
    sp.add_argument("--key", required=True)
    sp = add("retention-check", _cmd_retention_check, "新しい送信で削除される最古のログ行の出力が保全されているか確認する（送信はしない）")
    sp.add_argument("--log-json", required=True, help="現在のログ全体（JSONまたは保存HTML。状態・CSV・メモを含むこと）")
    sp.add_argument("--will-add", type=int, default=1)
    sp.add_argument("--no-db", action="store_true", help="DB取込の有無を調べない")
    sp.add_argument("--no-record", action="store_true", help="log_retention.jsonlへ記録しない")
    sp = add("find-output", _cmd_find_output, "この投入の出力CSVが既にローカルにあるか探す（取得はしない）")
    sp.add_argument("--key", required=True)
    sp = add("import-output", _cmd_import_output, "出力を検証し、入力に無いASINが無ければ既存の取込でDBへ入れる（冪等）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--csv", default=None, help="省略時は処理済みフォルダーの「<実行ID>_…csv」（1件に特定できる場合）")
    sp.add_argument("--recheck", action="store_true", help="取込を再実行して件数が変わらない（重複なし）ことを確認する")
    sp = add("verify-output", _cmd_verify_output, "出力CSVのASINが入力に含まれるか、事後に確認する（送信結果と実行IDの対応の確認）")
    sp.add_argument("--key", required=True)
    sp.add_argument("--csv", required=True)
    return p


def main(argv: list[str] | None = None, *, confirm=None, factory=None) -> int:
    a = build_parser().parse_args(argv)
    if a.func is _cmd_plan_pool:
        return a.func(a, factory=factory, confirm=confirm)
    if a.func is _cmd_pool_history_check:
        return a.func(a, factory=factory)
    if a.func is _cmd_withdraw_duplicate:
        return a.func(a, confirm=confirm)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
