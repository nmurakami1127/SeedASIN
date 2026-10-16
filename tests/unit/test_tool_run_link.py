"""seller_ledger.tool_run_linkの純粋なロジック（UNIQUE制約違反の判定・変換）の単体テスト。
DBアクセスを伴う正常系の記録・実際のPostgreSQLでの制約違反はtests/integration/
test_seller_ledger.py参照。

2026-09-19、ユーザー報告のバグ調査を受けて追加（docs/DEVIATIONS.md #50参照）: 以前は
cli.py・tool_intake/log_ingest.pyの双方が`except IntegrityError`を無条件に「既に記録
済み」として扱っており、tool_execution_link以外の想定外のIntegrityErrorまで誤って
握りつぶし得た。record()が違反した制約名を確認し、uk_tool_execution_link以外は
そのまま再送出することを検証する。
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from seller_ledger.tool_run_link import (
    DuplicateToolRunError,
    ToolRunLinkService,
    _violates_unique_tool_run_constraint,
)


class _FakeDiag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _FakeOrig(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("fake DBAPI error")
        self.diag = _FakeDiag(constraint_name)


def _integrity_error(constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _FakeOrig(constraint_name))


def test_violates_unique_tool_run_constraint_true_for_matching_constraint():
    assert _violates_unique_tool_run_constraint(_integrity_error("uk_tool_execution_link")) is True


def test_violates_unique_tool_run_constraint_false_for_other_constraint():
    assert _violates_unique_tool_run_constraint(_integrity_error("some_other_constraint")) is False


def test_violates_unique_tool_run_constraint_false_when_diag_missing():
    """診断情報を持たない（.diag/.constraint_nameが無い）DBAPI例外でも例外を送出せず、
    安全側（DuplicateToolRunErrorへ変換しない＝素通し）に倒すこと。"""
    err = IntegrityError("INSERT ...", {}, Exception("no diag attribute"))
    assert _violates_unique_tool_run_constraint(err) is False


class _FakeConnection:
    def __init__(self, exc_to_raise: Exception) -> None:
        self._exc = exc_to_raise

    def execute(self, *args, **kwargs):
        raise self._exc


def test_record_converts_unique_violation_to_duplicate_error():
    conn = _FakeConnection(_integrity_error("uk_tool_execution_link"))
    with pytest.raises(DuplicateToolRunError) as exc_info:
        ToolRunLinkService().record(conn, "US", "SELLER1", "7170614")
    assert exc_info.value.marketplace == "US"
    assert exc_info.value.tool_run_id == "7170614"


def test_record_does_not_swallow_unrelated_integrity_error():
    """uk_tool_execution_link以外のIntegrityErrorは「既に記録済み」として握りつぶさず、
    そのまま呼び出し元へ伝播させること（ユーザー報告のバグ調査を受けた修正、
    docs/DEVIATIONS.md #50参照）。"""
    original = _integrity_error("some_unrelated_constraint")
    conn = _FakeConnection(original)
    with pytest.raises(IntegrityError) as exc_info:
        ToolRunLinkService().record(conn, "US", "SELLER1", "7170614")
    assert exc_info.value is original
