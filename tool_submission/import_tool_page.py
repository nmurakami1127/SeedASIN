"""統合ツールの画面アダプター（Playwrightの`Page`を受け取る。ブラウザの起動・ログインはここでは行わない）。

**検証状況**（2026-10-17、利用者がログイン済みの実画面のDOMを読み取って確認。読み取りのみで、SAVEは押していない）
- 入力画面（`/asin/main.php`）: 「3.ASINリストから情報取得」のテキスト欄は`textarea#asin_list`、そのセクションのSAVEは`button#add_asin_list`
  （入力があるまでdisabled。クリック処理は`js/main.js`で、2段階目がOFFで3段階目がONなら`alert`で停止し、ASIN（10桁/13桁）を重複除去して
  `./out/start_search4.php`へ`country: ""`（**国指定なし**）でPOSTし、応答に`end`を含めば「保存しました。」）。他のSAVE（`#add_keyword`・
  `#add_keyword_repeat`・`#add_store_url`）と国選択は操作しない。設定値は`input#asin_*`のチェックボックス（`asin_variation`等）。
- ログ画面（`/asin/logs.php`）: `tr[data-seq]`（`data-type`・`data-country`・`data-options`＝送信時の設定値）、`textarea.memo`、
  `button.save_memo`（クリックで`./db/save_log.php`へ`{text, file_name=trのid, log_seq=td.log-idの値}`をPOST）。
- **このアダプター（Playwright）自体の実画面での動作は未検証**（実際の送信は、利用者のログイン済みChromeを読み取って確認したDOMに合わせて
  別のブラウザ操作で行う）。

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


# 画面の設定値（読み取るだけで切り替えない）。表示名 → チェックボックスのセレクタ（実画面で確認済み）
VERIFIED_OPTION_TOGGLES = {
    "CNフラグ除外": "input#asin_cn_safe", "ライバル（出品者）数": "input#asin_sellers", "ランキング": "input#asin_ranking",
    "ブランド名": "input#asin_brand", "最安値": "input#asin_price", "重量、体積重量": "input#asin_size",
    "最安値セラーの出品者国": "input#asin_low_country", "品番": "input#asin_part_number", "型番": "input#asin_model_number",
    "バリエーション": "input#asin_variation", "他国のライバル（出品者）数": "input#asin_f_sellers",
    "他国のランキング": "input#asin_f_ranking", "他国の最安値と差益": "input#asin_f_price",
}


@dataclass
class Selectors:
    # ---- ログ画面（保存HTML・既存automation.pyで確認済み。実画面は未検証）
    log_url: str = LOG_URL
    row_by_seq: str = 'tr[data-seq="{seq}"]'
    memo_textarea: str = "textarea.memo"
    memo_save_button: str = "button.save_memo"
    # ---- 入力画面（実画面のDOMを読み取って確認済み。国指定はなく、他のSAVE・国選択は使わない）
    input_url: str | None = "https://import-tools.jp/asin/main.php"
    asin_textarea: str | None = "textarea#asin_list"  # 「3.ASINリストから情報取得」のテキスト欄
    asin_save_button: str | None = "button#add_asin_list"  # 同じセクションのSAVE
    fill_timeout_ms: int = 180000  # 5,000件の貼り付けは30秒を超え得る（SAVE前なので未送信。DEVIATIONS #95）
    option_toggles: dict[str, str] = field(default_factory=lambda: dict(VERIFIED_OPTION_TOGGLES))  # 設定値の表示名 → チェックボックス（バリエーションは必須）


class ImportToolPage:
    def __init__(self, page, selectors: Selectors | None = None) -> None:
        self._p = page
        self._s = selectors or Selectors()

    # ---- 実行できるか
    def preflight(self) -> list[str]:
        s, out = self._s, []
        for name in ("input_url", "asin_textarea", "asin_save_button"):
            if not getattr(s, name):
                out.append(f"入力画面のセレクタ/URL（{name}）が未設定です")
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

    def log_html(self) -> str:
        """直前に読んだログ画面のHTML（既存のingest-logを1行に限定して実行するために使う）。"""
        return self._p.content()

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
            box.fill(text_, timeout=s.fill_timeout_ms)
            if box.input_value().strip() != text_:  # 入力が反映されていなければ、押さない
                raise SubmitFailed("BEFORE_CLICK", "テキスト欄に入力した内容を確認できません")
        except SubmitFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - クリック前の失敗（何も送っていない）
            raise SubmitFailed("BEFORE_CLICK", f"{type(exc).__name__}: {exc}") from exc
        cap: dict = {"alerts": []}

        def on_request(req) -> None:  # 送信リクエストの本文（実行IDの対応根拠）。認証情報・Cookieは読まない・残さない
            if str(req.url).split("?")[0].endswith("start_search4.php") and req.method == "POST":
                cap["endpoint"], cap["body"] = req.url.split("?")[0], req.post_data or ""

        def on_response(resp) -> None:
            if str(resp.url).split("?")[0].endswith("start_search4.php"):
                cap["status"] = resp.status
                try:
                    cap["response"] = resp.text()
                except Exception:  # noqa: BLE001
                    cap["response"] = ""

        def on_dialog(dialog) -> None:
            cap["alerts"].append(dialog.message[:80])
            dialog.dismiss()

        try:
            self._p.on("request", on_request)
            self._p.on("response", on_response)
            self._p.on("dialog", on_dialog)
        except Exception:  # noqa: BLE001 - 疑似ページ等で購読できない場合は根拠なし（呼び出し側が断定しない）
            pass
        try:
            self._p.locator(s.asin_save_button).click(no_wait_after=True)  # SAVEは1回だけ。待機のタイムアウトで再クリックしない
        except Exception as exc:  # noqa: BLE001 - クリックしたかもしれない
            raise SubmitFailed("UNKNOWN", f"{type(exc).__name__}: {exc}") from exc
        try:  # 遅い応答を最大120秒待つ。ログ画面への移動で応答を中断しない。SAVEは繰り返さない。
            for _ in range(240):
                if "response" in cap:
                    break
                self._p.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for event, callback in (("request", on_request), ("response", on_response), ("dialog", on_dialog)):
                try:
                    self._p.remove_listener(event, callback)
                except Exception:  # noqa: BLE001 - 疑似ページ・切断済みページ
                    pass
        # 部分的な記録も残す。成功判定は従来どおり本文・応答の両方を検証する。
        return SubmitResponse(evidence=cap)
