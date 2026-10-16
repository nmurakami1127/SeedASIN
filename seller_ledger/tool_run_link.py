"""tool_execution_linkの記録: セラーID ⇔ 統合ツール実行IDの対応を記録する
（docs/phase3_implementation_guide.md 3節）。

統合ツール実行IDは出力ファイル名から抽出した値（例: '7128337'）を呼び出し元が渡す想定。
tool_execution_link.tool_run_idは(marketplace, tool_run_id)でUNIQUE制約
（uk_tool_execution_link）があるため、同一tool_run_idの二重記録はDBレベルで防止される。

record()はこのUNIQUE制約違反のみをDuplicateToolRunErrorとして送出し、それ以外の
IntegrityError（本来想定していない失敗）はそのまま呼び出し元へ伝播させる
（2026-09-19修正、docs/DEVIATIONS.md #50参照）。以前は呼び出し元（cli.py・
tool_intake/log_ingest.py）がIntegrityErrorを無条件に「既に記録済み」として
握りつぶしており、tool_execution_link以外の想定外の失敗（テーブル構成の変更等で
将来UNIQUE制約以外のIntegrityErrorが起き得る場合）まで誤って「スキップ」扱いに
なり得るという指摘を受けて、違反した制約名（uk_tool_execution_link）を明示的に
確認する方式に修正した。
"""
from __future__ import annotations

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

_UNIQUE_CONSTRAINT_NAME = "uk_tool_execution_link"


class DuplicateToolRunError(Exception):
    """tool_run_idが(marketplace, tool_run_id)のUNIQUE制約（uk_tool_execution_link）に
    違反した場合にのみ送出する。呼び出し元はこれを「既に記録済み」として安全にスキップ
    してよい——それ以外のIntegrityErrorはrecord()がそのまま再送出するため、ここに
    到達する時点で原因は必ずこのUNIQUE制約であることが保証されている。"""

    def __init__(self, marketplace: str, tool_run_id: str) -> None:
        self.marketplace = marketplace
        self.tool_run_id = tool_run_id
        super().__init__(f"marketplace={marketplace} tool_run_id={tool_run_id} is already recorded")


def _violates_unique_tool_run_constraint(exc: IntegrityError) -> bool:
    """psycopgが報告する違反制約名（diag.constraint_name）で判定する（`e.orig.sqlstate`
    だけでは同じ23505でも別の一意制約と区別できないため）。診断情報が取得できない
    （DBAPIの違い等）場合は安全側に倒し、DuplicateToolRunErrorへ変換せず素通しする。"""
    diag = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    return constraint_name == _UNIQUE_CONSTRAINT_NAME


class ToolRunLinkService:
    def record(
        self, conn: Connection, marketplace: str, seller_id: str, tool_run_id: str, source: str | None = None
    ) -> None:
        """人が統合ツールを実行した後、そのtool_run_idとセラーの対応を記録する。

        sourceは発見経路（'SELLER_PAGE' / 'BRAND_STORE' / 'COMPETITOR' 等）。
        SellerService.register()の同種のdiscovery_source引数と同じ理由（どのロジックからも
        参照されない）でseller_ledger.cli link-tool-run・対話式メニューcli.pyからは廃止し、
        省略可能にした（ユーザー依頼、docs/DEVIATIONS.md #48参照。#25とは異なり、この引数
        自体はtool_intake/log_ingest.pyが統合ツールのログHTMLから自動記録する用途で
        引き続き使うため、SellerService.register()の時のように引数自体を削除はしていない）。
        省略時はDBのtool_execution_link.sourceがNULLになる（alembic/versions/0013で
        NOT NULL制約を外し済み）。

        既に同じ(marketplace, tool_run_id)が記録済みならDuplicateToolRunErrorを送出する。
        それ以外のIntegrityErrorはそのまま再送出する（#50参照）。
        """
        try:
            conn.execute(
                text(
                    """
                    INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source)
                    VALUES (:marketplace, :seller_id, :tool_run_id, :source)
                    """
                ),
                {
                    "marketplace": marketplace,
                    "seller_id": seller_id,
                    "tool_run_id": tool_run_id,
                    "source": source,
                },
            )
        except IntegrityError as exc:
            if _violates_unique_tool_run_constraint(exc):
                raise DuplicateToolRunError(marketplace, tool_run_id) from exc
            raise
