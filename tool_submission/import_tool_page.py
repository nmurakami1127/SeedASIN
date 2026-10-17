"""統合ツールの画面アダプター（Playwrightの`Page`を受け取る。ブラウザの起動・ログインはここでは行わない）。

**検証状況（セレクタを推測して検証済みとしない）**
- ログ画面（`logs.php`）: `tr[data-seq]`・`textarea.memo`・`td.log-status`・`td.log-csv`・メモの保存ボタン`button.save_memo`は、保存HTML
  （tests/unit/test_log_ingest.pyの実例）と既存の自動化（import_tools_automation/automation.pyが`tr[data-seq]`・`textarea.memo`を使用）で
  確認できる。ただし**実画面ではこのアダプターでは未検証**。
- 入力画面（「3.ASINリストから情報取得」のテキスト欄・SAVE・設定のトグル・画面のURL）: **DOMが保存資料に無いので未設定**。
  `Selectors`に利用者が確認した値を入れるまで`preflight()`が実行不可を返し、送信しない。国選択（上部のキーワード検索用）は一切操作しない。

操作の原則: SAVEは**1回だけ**押し、タイムアウトしても再クリックしない（クリック後の失敗は`UNKNOWN`として呼び出し側が照合する）。
設定値（CNフラグ除外・詳細2/3段階目・バリエーション）は**読み取るだけ**で、切り替えない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tool_submission.model import MAX_ASINS_PER_INPUT, SubmissionItem
from tool_submission.reconcile import LogEntry, LogUnreadable, parse_log_entries
from tool_submission.runner import SubmitFailed, SubmitResponse

LOG_URL = "https://import-tools.jp/asin/logs.php"
LOG_HEADER_MARKERS = ("CSV名", "メモ", "DL", "取得日時")  # 既存のautomation.pyが構造変化の検出に使う列名


@dataclass
class Selectors:
    # ---- ログ画面（保存HTML・既存automation.pyで確認済み。実画面は未検証）
    log_url: str = LOG_URL
    row_by_seq: str = 'tr[data-seq="{seq}"]'
    memo_textarea: str = "textarea.memo"
    memo_save_button: str = "button.save_memo"
    # ---- 入力画面（DOM未確認。利用者が確認するまで未設定）
    input_url: str | None = None
    asin_textarea: str | None = None  # 「3.ASINリストから情報取得」のテキスト欄
    asin_save_button: str | None = None  # 同じセクションのSAVE
    option_toggles: dict[str, str] = field(default_factory=dict)  # 画面の設定値の表示名 → トグルのセレクタ（バリエーションは必須）


class ImportToolPage:
    def __init__(self, page, selectors: Selectors | None = None) -> None:
        self._p = page
        self._s = selectors or Selectors()

    # ---- 実行できるか
    def preflight(self) -> list[str]:
        s, out = self._s, []
        for name in ("input_url", "asin_textarea", "asin_save_button"):
            if not getattr(s, name):
                out.append(f"入力画面のセレクタ/URL（{name}）が未設定です（DOMが未確認のため推測しません）")
        if not any("バリエーション" in k or "variation" in k.lower() for k in s.option_toggles):
            out.append("バリエーションのトグルのセレクタが未設定です（ONでないことを確認できないため送信しません）")
        return out

    # ---- ログ画面
    def read_log(self) -> list[LogEntry]:
        try:
            self._p.goto(self._s.log_url, wait_until="domcontentloaded")
            self._p.locator("table tr").first.wait_for(state="visible", timeout=30000)
            header = self._p.locator("table tr").first.inner_text()
            html = self._p.content()
        except Exception as exc:  # noqa: BLE001 - 未ログイン・通信失敗・構造変化は「読めない」として、再送しない判断に使う
            raise LogUnreadable(f"{type(exc).__name__}: {exc}") from exc
        if not all(m in header for m in LOG_HEADER_MARKERS):
            raise LogUnreadable("ログ一覧の構造が想定と違います（未ログインまたは画面の変更）")
        return parse_log_entries(html)

    def set_memo(self, run_id: int, memo: str) -> None:
        row = self._p.locator(self._s.row_by_seq.format(seq=run_id))
        row.locator(self._s.memo_textarea).fill(memo)
        row.locator(self._s.memo_save_button).click(no_wait_after=True)  # 保存の確認は呼び出し側が再読込で行う

    # ---- 入力画面
    def read_options(self) -> dict[str, bool]:
        """画面の設定値を読み取る（切り替えない）。"""
        if not self._s.input_url or not self._s.option_toggles:
            raise RuntimeError("設定値のセレクタが未設定です")
        self._p.goto(self._s.input_url, wait_until="domcontentloaded")
        return {label: bool(self._p.locator(sel).first.is_checked()) for label, sel in self._s.option_toggles.items()}

    def submit(self, item: SubmissionItem, input_path: Path) -> SubmitResponse | None:
        s = self._s
        if not (s.input_url and s.asin_textarea and s.asin_save_button):
            raise SubmitFailed("BEFORE_CLICK", "入力画面のセレクタが未設定です")
        asins = [a for a in Path(input_path).read_text(encoding="utf-8").splitlines() if a.strip()]
        if not asins or len(asins) > MAX_ASINS_PER_INPUT:
            raise SubmitFailed("BEFORE_CLICK", f"ASINが0件、または{MAX_ASINS_PER_INPUT}件を超えています: {len(asins)}")
        if len(asins) != item.asin_count:
            raise SubmitFailed("BEFORE_CLICK", f"入力ファイルのASIN数({len(asins)})が計画({item.asin_count})と違います")
        text_ = "\n".join(asins)
        try:
            self._p.goto(s.input_url, wait_until="domcontentloaded")
            box = self._p.locator(s.asin_textarea)
            box.fill(text_)
            if box.input_value().strip() != text_:  # 入力が反映されていなければ、押さない
                raise SubmitFailed("BEFORE_CLICK", "テキスト欄に入力した内容を確認できません")
        except SubmitFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - クリック前の失敗（何も送っていない）
            raise SubmitFailed("BEFORE_CLICK", f"{type(exc).__name__}: {exc}") from exc
        try:
            self._p.locator(s.asin_save_button).click(no_wait_after=True)  # SAVEは1回だけ。待機のタイムアウトで再クリックしない
        except Exception as exc:  # noqa: BLE001 - クリックしたかもしれない
            raise SubmitFailed("UNKNOWN", f"{type(exc).__name__}: {exc}") from exc
        return None
