"""統合ツールへの投入計画のモデル・メモ規約・投入キー。

**ここでは統合ツールへ接続しない。** 入力ファイル（出典ごと・業務方向ごと）と投入計画を作り、投入の状態を追記専用の台帳に
残すための部品。統合ツールの画面操作は`runner.py`の抽象（`ToolPage`）の背後にあり、実画面のアダプターは未実装。

用語を混同しない:
- 取得先市場(`fetch_market`): ASINを集めたAmazonのページの市場。`collector_run.marketplace`
- 業務方向(`direction`): EXPORT(輸出=US) / IMPORT(輸入=JP)。**取得先市場からは決めない**。出典の発見経路(`acquisition_path.direction`)から決める
- メモ接頭辞: 輸出_ / 輸入_ （既存のログ取込`tool_intake/log_ingest.py`が業務方向と`tool_execution_link.seller_id`を決める規約。変えない）
- ツールの国設定(`tool_country`, ログ画面の`data-country`): 統合ツールがASINを調べる国。業務方向とは別の属性で、**自動では決めない**（未確定=DRAFT）
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

DIRECTION_PREFIX = {"EXPORT": "輸出", "IMPORT": "輸入"}
BUSINESS_MARKET = {"EXPORT": "US", "IMPORT": "JP"}  # tool_execution_link.marketplace（業務方向の市場）
MEMO_KEY_MAX = 40  # tool_execution_link.seller_id は String(40)


class SubmissionError(Exception):
    """投入計画を作れない（方向が決まらない・メモが長すぎる等）。"""


def build_memo(direction: str, source_key: str) -> str:
    """既存の規約どおり「輸出_<出典キー>」「輸入_<出典キー>」。出典キーが40文字を超えると既存の取込が格納できないので拒否する。"""
    if direction not in DIRECTION_PREFIX:
        raise SubmissionError(f"業務方向が不正です: {direction!r}")
    key = source_key.strip()
    if not key:
        raise SubmissionError("出典キーが空です")
    if len(key) > MEMO_KEY_MAX:
        raise SubmissionError(f"出典キーが{MEMO_KEY_MAX}文字を超えています（メモから取り込めません）: {key[:20]}…")
    return f"{DIRECTION_PREFIX[direction]}_{key}"


def submission_key(collector_run_id: int, direction: str, input_sha256: str) -> str:
    """同じ内容の再実行が同じキーになる（取得run・業務方向・入力ファイルの内容で決まる）。"""
    return hashlib.sha256(f"v1|{collector_run_id}|{direction}|{input_sha256}".encode()).hexdigest()[:16]


def asin_file_bytes(asins: list[str]) -> bytes:
    """統合ツールの「ASINリスト」入力用: ヘッダー無し・1行1ASIN・UTF-8（asin_collector.cli export-runと同じ形式）。"""
    return ("\n".join(asins) + ("\n" if asins else "")).encode("utf-8")


@dataclass
class SubmissionItem:
    key: str
    collector_run_id: int
    fetch_market: str
    source_id: int
    source_type: str
    source_key: str
    direction: str
    business_market: str
    memo: str
    tool_country: str | None  # 'jp' / 'us' / None（未確定）
    run_outcome: str
    input_is_partial: bool
    partial_reason: str | None
    run_completed_at: str
    run_pages: int | None
    asin_count: int
    total_rows: int
    duplicate_rows_removed: int
    input_file: str
    input_sha256: str
    origin: dict | None  # 代表起点（import・ASIN・注文点数・市場）
    paths: list[dict] = field(default_factory=list)  # この業務方向の全ての経路（起点・経由出典・経由ASIN・調査先市場）
    other_directions: list[str] = field(default_factory=list)  # 同じ取得runを別の方向でも使う場合の他の方向
    prior_tool_runs: list[str] = field(default_factory=list)  # 計画時点で既に`tool_execution_link`にある、同じ業務方向・同じメモ文字列の統合ツール実行ID（照合で新しい実行と取り違えない）

    @property
    def status(self) -> str:
        return "READY" if self.tool_country else "DRAFT"  # DRAFT = ツールの国設定が未確定（利用者の確認待ち）
