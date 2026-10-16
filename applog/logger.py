"""M12 構造化ログ出力（実装指示書Phase 2）。

出力先は初期実装では標準出力JSON（1行1レコードのJSON Lines）。監視ツール連携は将来対応。
CLAUDE.md 10節の管理操作監査ログ（event="ADMIN_..."）もこのクラスでそのまま出力する。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredLogger:
    def __init__(self, module: str, marketplace: str | None = None) -> None:
        self._module = module
        self._marketplace = marketplace

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "module": self._module,
            "marketplace": self._marketplace,
            "event": event,
            **fields,
        }
        print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)
