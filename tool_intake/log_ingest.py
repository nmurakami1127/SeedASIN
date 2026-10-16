"""統合ツールのログ画面を「ページの保存」（HTMLのみ）で保存したファイルを読み込み、
seller_ledger.register/link-tool-runとtool_intake.import-tool-outputをまとめて
自動実行する（ユーザー依頼、2026-09-06。docs/DEVIATIONS.md #26参照）。

seller_ledgerは「セラーID」に限らず、統合ツールに投入した文字列（ブランド名・
キーワード・ASINリスト等）の実績を追う汎用システムとして扱う
（known_seller_master.seller_idにセラーID以外の文字列も入る想定、ユーザー確認済み。
テーブル名・クラス名自体は変更しない）。

行の識別は`<tr>`の`data-seq`属性（統合ツール実行ID）のみを使う。`id`属性は
"none"のような意味の無い値が実データに存在するため一切参照しない
（ユーザー指摘、2026-09-06）。

marketplace/seller_idの決定ルール（メモ欄=<textarea class="memo">の中身で判定）:
1. メモが「輸出」または「輸入」で始まる場合、marketplace=US（輸出）/JP（輸入）、
   seller_id=プレフィックス以降の文字列。プレフィックス直後の区切り文字「_」は
   あれば1つだけ除去し、無くてもそのまま受け付ける（実データに「輸入A1VX7O8D3UFTPU」
   のように区切りの_が欠落した表記ゆれが存在するため、ユーザー指摘、2026-09-06）。
2. 上記のプレフィックスが無い場合（純粋なキーワード検索等）、marketplace=
   data-country属性（jp→JP, us→US）、seller_id=メモの中身そのまま。
   **data-country属性はこの分岐でのみ参照する**（ルール1の行では一切見ない）。

log-statusとlog-csvの組み合わせによるimport-tool-output実行可否:
- log-status="取得完了" かつ log-csvが"-"でない（ファイル名がある）場合のみ
  import-tool-outputを試みる。それ以外（禁止セラー・待機中・取得中等、いずれも
  log-csv="-"になる）は自然にスキップされる。
- 対象ファイルはtool_output_dir（既定: C:\\work\\output\\primary_check）配下を探す。
  見つからない場合はエラーにせず「ファイル未検出」として記録し処理を継続する。

既にlink-tool-run済み（tool_execution_linkのUNIQUE制約違反、
ToolRunLinkService.DuplicateToolRunError）の行はエラーで落とさず、スキップして
次の判定（import-tool-output）へ進む（CLAUDE.md 6節のエラー分離、ユーザー確認済み）。
DuplicateToolRunErrorのみを捕捉し、それ以外のIntegrityError（本来想定していない
失敗）はそのまま`ingest_log()`の呼び出し元へ伝播させる——以前は`except IntegrityError`
で無条件に「既に記録済み」としてスキップしていたため、tool_execution_link以外の
想定外の失敗まで誤って握りつぶし得るという指摘を受けて修正した
（2026-09-19、docs/DEVIATIONS.md #50参照）。

log-status="禁止セラー"の行は、register/link-tool-runに加えてstorefront_blocked_seller
への記録（reason='PERMANENTLY_EXCLUDED'固定。旧HIGH_RISK_COUNTRYをリネーム・意味を
拡張したもの、docs/DEVIATIONS.md #30・#41参照）も行う。register/link-tool-runとは
別の独立したトランザクションで実行する——同じ
ログファイルを再取込した際、既にlink-tool-run済み（DuplicateToolRunError）でも
storefront_blocked_sellerへの記録が未実施なら記録されるようにするため（自己修復的に、
再実行のたびに両方の状態を正しい状態へ収束させる）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from selectolax.parser import HTMLParser

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_ledger.tool_run_link import DuplicateToolRunError, ToolRunLinkService
from tool_intake.csv_utils import open_text_auto
from tool_intake.integration_tool_import import IntegrationToolImportService

DEFAULT_TOOL_OUTPUT_DIR = Path(r"C:\work\output\primary_check")

_PREFIX_TO_MARKETPLACE = {"輸出": "US", "輸入": "JP"}
_COUNTRY_TO_MARKETPLACE = {"jp": "JP", "us": "US"}


@dataclass
class LogRow:
    tool_run_id: str
    marketplace: str
    seller_id: str
    source: str
    status: str
    csv_filename: str | None


@dataclass
class ParseError:
    tool_run_id: str
    reason: str


@dataclass
class IngestSummary:
    total_rows: int = 0
    parse_errors: int = 0
    new_sellers: int = 0
    existing_sellers: int = 0
    already_linked: int = 0
    forbidden_sellers: int = 0
    storefront_blocked_recorded: int = 0
    storefront_blocked_already_recorded: int = 0
    imports_attempted: int = 0
    imports_succeeded: int = 0
    files_not_found: int = 0
    other_errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"処理行数: {self.total_rows}件（解析エラー: {self.parse_errors}件）",
            f"セラー登録: 新規{self.new_sellers}件 / 既存{self.existing_sellers}件 / "
            f"既にリンク済みでスキップ{self.already_linked}件",
            f"禁止セラー: {self.forbidden_sellers}件"
            f"（ストアフロント取得不可として新規記録{self.storefront_blocked_recorded}件 / "
            f"記録済みでスキップ{self.storefront_blocked_already_recorded}件）",
            f"取込試行: {self.imports_attempted}件（成功{self.imports_succeeded}件 / "
            f"ファイル未検出{self.files_not_found}件）",
            f"その他エラー: {self.other_errors}件",
        ]
        lines.extend(f"  - {detail}" for detail in self.error_details)
        return "\n".join(lines)


def _resolve_marketplace_and_seller_id(memo: str, country: str) -> tuple[str | None, str]:
    memo = memo.strip()
    for prefix, marketplace in _PREFIX_TO_MARKETPLACE.items():
        if memo.startswith(prefix):
            seller_id = memo[len(prefix) :].lstrip("_").strip()
            return marketplace, seller_id
    # プレフィックス無し（ルール2）: この分岐でのみdata-country属性を参照する。
    return _COUNTRY_TO_MARKETPLACE.get(country.strip().lower()), memo


def parse_log_html(html: str) -> tuple[list[LogRow], list[ParseError]]:
    """ログ画面HTMLから対象行を抽出する。data-seq属性を持つ<tr>のみが対象
    （theadの<tr>にはdata-seqが無いため自然に除外される）。"""
    tree = HTMLParser(html)
    rows: list[LogRow] = []
    errors: list[ParseError] = []

    for tr in tree.css("tr[data-seq]"):
        tool_run_id = (tr.attributes.get("data-seq") or "").strip()
        country = tr.attributes.get("data-country") or ""
        source = (tr.attributes.get("data-type") or "").strip().upper()

        memo_node = tr.css_first("textarea.memo")
        memo = memo_node.text().strip() if memo_node else ""

        status_node = tr.css_first("td.log-status")
        status = status_node.text().strip() if status_node else ""

        csv_node = tr.css_first("td.log-csv")
        csv_text = csv_node.text().strip() if csv_node else "-"
        csv_filename = None if csv_text in ("", "-") else csv_text

        if not tool_run_id:
            errors.append(ParseError(tool_run_id="(不明)", reason="data-seq属性が空です"))
            continue

        marketplace, seller_id = _resolve_marketplace_and_seller_id(memo, country)
        if marketplace is None:
            errors.append(
                ParseError(
                    tool_run_id=tool_run_id,
                    reason=f"marketplaceを判定できません（メモ='{memo}', data-country='{country}'）",
                )
            )
            continue
        if not seller_id:
            errors.append(ParseError(tool_run_id=tool_run_id, reason="seller_idが空です"))
            continue

        rows.append(
            LogRow(
                tool_run_id=tool_run_id,
                marketplace=marketplace,
                seller_id=seller_id,
                source=source or "UNKNOWN",
                status=status,
                csv_filename=csv_filename,
            )
        )

    return rows, errors


def _process_row(
    session_factory: SessionFactory,
    row: LogRow,
    tool_output_dir: Path,
    summary: IngestSummary,
) -> None:
    if row.status == "禁止セラー":
        summary.forbidden_sellers += 1

    try:
        with session_factory.get_session(row.marketplace) as conn, conn.begin():
            judgement = SellerService().register(conn, row.marketplace, row.seller_id)
            ToolRunLinkService().record(conn, row.marketplace, row.seller_id, row.tool_run_id, row.source)
        if judgement == "NEW":
            summary.new_sellers += 1
        else:
            summary.existing_sellers += 1
    except DuplicateToolRunError:
        # 既にlink-tool-run済み（tool_execution_linkのUNIQUE制約違反）。
        # エラーにせずスキップし、import-tool-output判定へ進む。
        summary.already_linked += 1

    if row.status == "禁止セラー":
        with session_factory.get_session(row.marketplace) as conn, conn.begin():
            block_result = StorefrontBlockedSellerService().mark_blocked(
                conn, row.seller_id, "PERMANENTLY_EXCLUDED"
            )
        if block_result.status == "NEW":
            summary.storefront_blocked_recorded += 1
        else:
            summary.storefront_blocked_already_recorded += 1

    if row.status == "取得完了" and row.csv_filename:
        summary.imports_attempted += 1
        file_path = tool_output_dir / row.csv_filename
        if not file_path.exists():
            summary.files_not_found += 1
            return
        IntegrationToolImportService().import_file(
            session_factory, row.marketplace, row.tool_run_id, file_path
        )
        summary.imports_succeeded += 1


def ingest_log(
    session_factory: SessionFactory,
    html_path: Path,
    tool_output_dir: Path = DEFAULT_TOOL_OUTPUT_DIR,
) -> IngestSummary:
    html = open_text_auto(html_path)
    rows, parse_errors = parse_log_html(html)

    summary = IngestSummary(total_rows=len(rows) + len(parse_errors), parse_errors=len(parse_errors))
    for error in parse_errors:
        summary.error_details.append(f"[解析エラー] tool_run_id={error.tool_run_id}: {error.reason}")

    for row in rows:
        try:
            _process_row(session_factory, row, tool_output_dir, summary)
        except Exception as exc:  # 1行のエラーで全体を止めない（CLAUDE.md 6節）
            summary.other_errors += 1
            summary.error_details.append(f"tool_run_id={row.tool_run_id}: {exc}")

    return summary
