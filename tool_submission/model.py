"""統合ツールへの投入計画のモデル・メモ規約・投入キー。

**ここでは統合ツールへ接続しない。** 入力ファイル（出典ごと・業務方向ごと・5,000件以下ごと）と投入計画を作り、投入の状態を
追記専用の台帳に残すための部品。統合ツールの画面操作は`runner.py`の抽象（`ToolPage`）の背後にある。

使うのは統合ツールの「3.ASINリストから情報取得（ライバルチェッカー）」: テキスト欄にASINを改行区切りで入力し、そのセクションの
SAVEを押す。**このASINリスト入力に国指定はない**（上部のキーワード検索用の国選択は操作しない）。メモはログ画面で**投入の後に**
付ける。1回の入力は5,000件まで（画面の案内）。

用語を混同しない:
- 取得先市場(`fetch_market`): ASINを集めたAmazonのページの市場。`collector_run.marketplace`
- 業務方向(`direction`): EXPORT(輸出=US) / IMPORT(輸入=JP)。**どの意図で探索・取得したかという経路**から決める。取得先市場や
  ログの`data-country`からは決めない
- メモ接頭辞: 輸出_ / 輸入_ （既存のログ取込`tool_intake/log_ingest.py`が業務方向と`tool_execution_link.seller_id`を決める規約。変えない）
- 国設定がないことと、取得先市場・業務方向を記録しなくてよいことは別: これらは計画・台帳・マニフェスト・経路に残す
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

DIRECTION_PREFIX = {"EXPORT": "輸出", "IMPORT": "輸入"}
BUSINESS_MARKET = {"EXPORT": "US", "IMPORT": "JP"}  # tool_execution_link.marketplace（業務方向の市場）
MEMO_KEY_MAX = 40  # tool_execution_link.seller_id は String(40)
MAX_ASINS_PER_INPUT = 5000  # 画面の案内「5,000件ずつ登録ください」


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
    """同じ内容の再実行が同じキーになる（取得run・業務方向・入力ファイルの内容で決まる。分割した入力は内容が違うので別のキー）。"""
    return hashlib.sha256(f"v1|{collector_run_id}|{direction}|{input_sha256}".encode()).hexdigest()[:16]


def asin_file_bytes(asins: list[str]) -> bytes:
    """統合ツールの「ASINリスト」入力用: ヘッダー無し・1行1ASIN・UTF-8（asin_collector.cli export-runと同じ形式）。"""
    return ("\n".join(asins) + ("\n" if asins else "")).encode("utf-8")


def split_asins(asins: list[str], size: int = MAX_ASINS_PER_INPUT) -> list[list[str]]:
    """出典・業務方向ごとの入力を、保存順のまま size 件以下に分割する。"""
    if size < 1:
        raise ValueError("size must be >= 1")
    return [asins[i:i + size] for i in range(0, len(asins), size)] or [[]]


# 画面の設定値（読み取って記録するだけ。無断で切り替えない）。バリエーションは子ASIN取得が大幅に増え得る（画面の説明: 1IDで3〜4日）ので、
# ONのときは送信しない。それ以外（CNフラグ除外・詳細2段階目/3段階目）は今回の国指定とは別のオプションで、現在値を記録するだけ。
VARIATION_KEYS = ("バリエーション", "variation")


def option_blockers(options: dict[str, bool] | None) -> list[str]:
    out = []
    for k, v in (options or {}).items():
        if any(x in k.lower() for x in (s.lower() for s in VARIATION_KEYS)) and v:
            out.append(f"{k}がONです（子ASINの取得が大幅に増える可能性。自動では切り替えません。画面で確認してください）")
    return out


def item_from_manifest(m: dict) -> "SubmissionItem":
    """台帳のマニフェスト（計画）から投入項目を復元する（将来の項目追加・削除に備え、既知の項目だけを使う）。"""
    names = set(SubmissionItem.__dataclass_fields__)
    return SubmissionItem(**{k: v for k, v in m.items() if k in names})


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
    run_outcome: str
    input_is_partial: bool
    partial_reason: str | None
    run_completed_at: str
    run_pages: int | None
    asin_count: int  # この入力ファイル（分割後）のASIN数
    total_rows: int  # 取得runの行数（分割前・重複除去前）
    duplicate_rows_removed: int
    input_file: str
    input_sha256: str
    origin: dict | None  # 代表起点（import・ASIN・注文点数・市場）
    paths: list[dict] = field(default_factory=list)  # この業務方向の全ての経路（起点・経由出典・経由ASIN・調査先市場）
    other_directions: list[str] = field(default_factory=list)  # 同じ取得runを別の方向でも使う場合の他の方向
    chunk_index: int = 1  # 5,000件を超えるときの分割（1始まり）
    chunk_count: int = 1
    run_asin_total: int = 0  # 取得run全体の（重複除去後の）ASIN数
    prior_tool_runs: list[str] = field(default_factory=list)  # 計画時点で既に`tool_execution_link`にある、同じ業務方向・同じ文字列の実行ID（情報のみ。照合の除外には使わない）
    screen_options: dict | None = None  # 送信直前に画面から読み取った設定値（計画時は未取得）

    @property
    def status(self) -> str:
        return "READY"  # ASINリスト入力に国設定は不要。送信の可否は台帳・画面の確認（バリエーションOFF等）で決まる
