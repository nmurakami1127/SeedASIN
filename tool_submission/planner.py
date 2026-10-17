"""取得済みの`collector_run`から、統合ツール用の入力ファイル（出典ごと・業務方向ごと）と投入計画を作る。

**DBは読み取りだけ**（実DBへの対応記録はスキーマが無い。台帳はファイル）。入力にできる取得は、`COMPLETED`で結果が
`SUCCESS_NONEMPTY` / `SUCCESS_TRUNCATED` のものだけ（PARTIAL・失敗・未確認の0件は使わない）。上限打切りの取得は
`input_is_partial=true`（一部の取得結果からの入力）と理由を計画に明記する。

出典ごとに入力を分け、混合投入しない。業務方向は**出典の発見経路**(`acquisition_path.direction`)から決め、取得先市場からは
決めない。1つの取得runに複数の方向の経路があれば、方向ごとに別の入力・別のメモにして、それぞれの対応を残す（同じASINの
重複は入力ファイルの中で1回にするが、出典・経路の証拠はマニフェストに全部残す）。方向が決まらない出典（経路が手動・起点なし）は、
利用者が`--direction`で明示するまで計画しない。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import text

from db.session import SessionFactory
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import (
    BUSINESS_MARKET, SubmissionError, SubmissionItem, asin_file_bytes, build_memo, submission_key,
)

USABLE_OUTCOMES = ("SUCCESS_NONEMPTY", "SUCCESS_TRUNCATED")
PARTIAL_REASON = {"SUCCESS_TRUNCATED": "上限打切りの取得（一部のASINのみ。ページ数・ASIN数の上限で止めた）"}


def _path_dict(p) -> dict:
    return {"stage": p.stage, "direction": p.direction, "origin_market": p.origin_marketplace, "origin_import_id": p.origin_import_id,
            "origin_asin": p.origin_asin, "origin_units": p.origin_units, "via_source_id": p.via_source_id, "via_asin": p.via_asin}


def build_items(app: SessionFactory, fetch_market: str, run_id: int, *, tool_country: str | None, out_dir: Path,
                direction_override: str | None = None) -> tuple[list[SubmissionItem], list[bytes]]:
    """計画（DRAFT/READY）と、各入力ファイルの内容を返す。書き込みはしない。"""
    if tool_country is not None and tool_country.lower() not in ("jp", "us"):
        raise SubmissionError("tool_country は jp / us で指定してください")
    with app.get_session(fetch_market) as conn, conn.begin():
        run = conn.execute(
            text("SELECT id, marketplace, source_id, status, outcome, truncation_reason, pages_fetched, completed_at "
                 "FROM collector_run WHERE id = :id AND source_id IS NOT NULL"), {"id": run_id}).one_or_none()
        if run is None:
            raise SubmissionError(f"run={run_id} は市場{fetch_market}の出典取得として存在しません")
        if run.status != "COMPLETED" or run.outcome not in USABLE_OUTCOMES:
            raise SubmissionError(f"run={run_id} は入力に使えません（状態={run.status} 結果={run.outcome}。成功または上限打切りのみ）")
        src = conn.execute(
            text("SELECT source_id, source_type, key_primary, display_primary FROM acquisition_source WHERE source_id = :s"),
            {"s": run.source_id}).one()
        paths = conn.execute(
            text("SELECT stage, direction, origin_marketplace, origin_import_id, origin_asin, origin_units, via_source_id, via_asin "
                 "FROM acquisition_path WHERE source_id = :s ORDER BY path_id"), {"s": run.source_id}).all()
        raw = [r[0] for r in conn.execute(text("SELECT asin FROM collector_asin WHERE collector_run_id = :r ORDER BY id"), {"r": run_id})]
    asins = list(dict.fromkeys(a.strip().upper() for a in raw if a and a.strip()))
    if not asins:
        raise SubmissionError(f"run={run_id} にはASINがありません")
    directions = sorted({p.direction for p in paths if p.direction})
    if direction_override:
        if direction_override not in BUSINESS_MARKET:
            raise SubmissionError("direction は EXPORT / IMPORT で指定してください")
        if directions and direction_override not in directions:
            raise SubmissionError(f"出典の経路の方向は{directions}です。経路と異なる方向は指定できません")
        directions = [direction_override]
    if not directions:
        raise SubmissionError(f"出典{src.source_id}の経路に業務方向がありません（手動登録・起点なし）。--direction で明示してください")
    content = asin_file_bytes(asins)
    sha = hashlib.sha256(content).hexdigest()
    key_name = src.key_primary if src.source_type == "SELLER" else src.display_primary
    partial = run.outcome == "SUCCESS_TRUNCATED"
    items, files = [], []
    for d in directions:
        dpaths = [p for p in paths if p.direction == d] or []
        best = max((p for p in dpaths if p.origin_units is not None), key=lambda p: (p.origin_units, p.origin_asin or ""), default=None)
        key = submission_key(run_id, d, sha)
        items.append(SubmissionItem(
            key=key, collector_run_id=run_id, fetch_market=fetch_market, source_id=src.source_id, source_type=src.source_type,
            source_key=key_name, direction=d, business_market=BUSINESS_MARKET[d], memo=build_memo(d, key_name),
            tool_country=tool_country.lower() if tool_country else None, run_outcome=run.outcome, input_is_partial=partial,
            partial_reason=PARTIAL_REASON.get(run.outcome), run_completed_at=run.completed_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            run_pages=run.pages_fetched, asin_count=len(asins), total_rows=len(raw), duplicate_rows_removed=len(raw) - len(asins),
            input_file=str(Path(out_dir) / f"run{run_id}_{d}_{key}.csv"), input_sha256=sha,
            origin=None if best is None else {"market": best.origin_marketplace, "import_id": best.origin_import_id,
                                              "asin": best.origin_asin, "units": best.origin_units},
            paths=[_path_dict(p) for p in dpaths], other_directions=[x for x in directions if x != d],
        ))
        files.append(content)
    return items, files


def write_plan(items: list[SubmissionItem], files: list[bytes], ledger: SubmissionLedger) -> list[tuple[SubmissionItem, str]]:
    """入力ファイルとマニフェストを書き、台帳にPLANNEDを残す（冪等: 同じ内容の再実行は何も増やさない）。"""
    results = []
    for item, content in zip(items, files):
        path = Path(item.input_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.input_sha256:
                raise SubmissionError(f"同名で内容の異なる入力ファイルがあります（上書きしません）: {path}")
        else:
            tmp = path.with_suffix(".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        manifest = asdict(item)
        manifest["status"] = item.status
        (path.parent / f"manifest_{item.key}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        st = ledger.state(item.key)
        if st.state in ("NONE", "PLANNED") and st.manifest != manifest:
            ledger.append(item.key, "PLANNED", manifest=manifest)
            results.append((item, "新規" if st.state == "NONE" else "更新"))
        else:
            results.append((item, "変更なし" if st.state in ("PLANNED",) else f"台帳の状態={st.state}（計画は変更しません）"))
    return results
