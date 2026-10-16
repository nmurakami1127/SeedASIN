"""システムエラー・業務エラーの例外階層。"""
from __future__ import annotations

from errorctl.error_codes import ErrorCode


class SeedAsinError(Exception):
    """本システム共通の基底例外。"""

    code: ErrorCode

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code.value)


class RLSViolationError(SeedAsinError):
    """app.current_marketplace未設定でのDBアクセス。システムエラーとして即時停止する（CLAUDE.md 2節）。"""

    code = ErrorCode.MP_11_01
