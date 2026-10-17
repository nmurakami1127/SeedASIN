"""送信リクエスト・応答の根拠（evidence）の検証と、既存DBへの対応記録（既存のingest-log・tool_execution_link）への薄い接続。

手動手順（cli: evidence / ingest / link-check）と、自動の流れ（runner・workflow）の両方が同じ判定を使う。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from sqlalchemy import text

# 実画面の応答（2026-10-17確認）: 成功時は "start_search4asin_listrivalsuccess asin<最古ID> https://…/csv_del.php<br>" のように"success"を含む。
# 失敗は session_expired / no_api / no_f_api / url_au / url_ca / ban / no_me / not_release / not_http（画面のスクリプトの分岐）。"end"は別の分岐。
FAILURE_TOKENS = ("session_expired", "no_api", "no_f_api", "url_au", "url_ca", "ban", "no_me", "not_release", "not_http")


def check_capture(plan_asins: list[str], cap: dict) -> dict:
    """capture: {"endpoint", "body"(送信したJSON文字列), "status", "response", "alerts"}。計画と一致し成功応答なら ok=True。"""
    problems: list[str] = []
    try:
        body = json.loads(cap["body"])
    except (KeyError, ValueError, TypeError):
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
    hit = [t for t in FAILURE_TOKENS if t in resp]
    if cap.get("status") != 200 or hit or not ("success" in resp or "end" in resp):
        problems.append(f"応答が保存成功ではありません（status={cap.get('status')}, 失敗の目印={hit}, 応答={resp[:80]!r}）")
    if (body.get("options") or {}).get("variation"):
        problems.append("送信リクエストのバリエーションがONです")
    return {"ok": not problems, "problems": problems, "asin_count": len(sent),
            "asin_list_sha256": hashlib.sha256(",".join(sent).encode()).hexdigest(),
            "request_options": body.get("options"), "response": resp[:120], "alerts": cap.get("alerts", [])}


def ingest_one_row(factory, run_id: int, memo: str, log_html: str, output_dir: Path | None = None):
    """既存のingest-logを、この実行IDの1行だけに限定して実行する（無関係な行・保留状態は取り込まない）。"""
    from tool_intake.log_ingest import ingest_log
    from tool_submission.reconcile import single_row_html

    html = single_row_html(log_html, int(run_id), memo)  # ValueError: 行が無い/メモが違う
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "one_row.html"
        p.write_text(html, encoding="utf-8")
        return ingest_log(factory, p, Path(output_dir)) if output_dir else ingest_log(factory, p)


def link_status(factory, business_market: str, tool_run_id: str, source_key: str) -> tuple[str, str | None]:
    """tool_execution_linkの記録を読む（DB読み取りのみ）。('LINKED'|'MISSING'|'MISMATCH', seller_id)"""
    with factory.get_session(business_market) as conn, conn.begin():
        row = conn.execute(text("SELECT seller_id, source FROM tool_execution_link WHERE marketplace = :m AND tool_run_id = :r"),
                           {"m": business_market, "r": tool_run_id}).one_or_none()
    if row is None:
        return "MISSING", None
    return ("LINKED" if row.seller_id == source_key else "MISMATCH"), row.seller_id
