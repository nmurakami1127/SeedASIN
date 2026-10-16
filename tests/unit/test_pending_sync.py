"""tool_intake.pending_syncの単体テスト（DB非依存）。find_pending・find_tool_output_files・
IntegrationToolImportServiceをモックし、PendingSyncService.sync()の分岐
（自動取込/未検出/エラー）とPendingSyncResult.format()の表示内容を検証する。

find_pending()自体のSQL（未取込/0件の判定）の検証はtests/integration/test_pending_sync.py参照。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tool_intake.pending_sync import PendingRun, PendingSyncResult, PendingSyncService


class _FakeSessionFactory:
    @contextmanager
    def get_session(self, marketplace):
        yield object()


def _run(tool_run_id: str, seller_id: str = "SELLER1") -> PendingRun:
    return PendingRun(
        seller_id=seller_id, tool_run_id=tool_run_id, requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc)
    )


def test_sync_imports_when_exactly_one_candidate_found(monkeypatch):
    pending = [_run("1234567")]
    monkeypatch.setattr(PendingSyncService, "find_pending", lambda self, conn, mp: pending)
    monkeypatch.setattr("tool_intake.pending_sync.find_tool_output_files", lambda mp, rid: [Path("1234567_a.csv")])

    called = {}

    class _FakeImportService:
        def import_file(self, factory, marketplace, tool_run_id, path):
            called["args"] = (marketplace, tool_run_id, path)

    monkeypatch.setattr("tool_intake.pending_sync.IntegrationToolImportService", _FakeImportService)

    result = PendingSyncService().sync(_FakeSessionFactory(), "US")

    assert called["args"] == ("US", "1234567", Path("1234567_a.csv"))
    assert result.imported == pending
    assert result.not_found == []
    assert result.errored == []


def test_sync_leaves_as_not_found_when_no_candidate(monkeypatch):
    pending = [_run("1234567")]
    monkeypatch.setattr(PendingSyncService, "find_pending", lambda self, conn, mp: pending)
    monkeypatch.setattr("tool_intake.pending_sync.find_tool_output_files", lambda mp, rid: [])

    result = PendingSyncService().sync(_FakeSessionFactory(), "US")

    assert result.imported == []
    assert result.not_found == pending


def test_sync_leaves_as_not_found_when_multiple_candidates_found(monkeypatch):
    """非対話のバッチ処理のため、複数候補は自動選択せず未検出扱いにする
    （tool_intake.cli import-tool-outputの--file省略時と同じ方針）。"""
    pending = [_run("1234567")]
    monkeypatch.setattr(PendingSyncService, "find_pending", lambda self, conn, mp: pending)
    monkeypatch.setattr(
        "tool_intake.pending_sync.find_tool_output_files",
        lambda mp, rid: [Path("1234567_a.csv"), Path("1234567_b.csv")],
    )

    result = PendingSyncService().sync(_FakeSessionFactory(), "US")

    assert result.not_found == pending
    assert result.imported == []


def test_sync_records_error_and_continues_when_import_raises(monkeypatch):
    """1件のtool_run_idの取込が例外を送出しても、他のtool_run_idの処理を止めない
    （CLAUDE.md 6節のエラー分離）。"""
    pending = [_run("1111111"), _run("2222222")]
    monkeypatch.setattr(PendingSyncService, "find_pending", lambda self, conn, mp: pending)
    monkeypatch.setattr("tool_intake.pending_sync.find_tool_output_files", lambda mp, rid: [Path(f"{rid}_a.csv")])

    class _FailingOnFirstImportService:
        def import_file(self, factory, marketplace, tool_run_id, path):
            if tool_run_id == "1111111":
                raise ValueError("ASIN列が空です")

    monkeypatch.setattr("tool_intake.pending_sync.IntegrationToolImportService", _FailingOnFirstImportService)

    result = PendingSyncService().sync(_FakeSessionFactory(), "US")

    assert [r.tool_run_id for r in result.imported] == ["2222222"]
    assert len(result.errored) == 1
    assert result.errored[0][0].tool_run_id == "1111111"
    assert "ASIN列が空です" in result.errored[0][1]


def test_format_shows_all_three_buckets():
    result = PendingSyncResult(
        imported=[_run("1111111")],
        not_found=[_run("2222222")],
        errored=[(_run("3333333"), "boom")],
    )
    output = result.format()

    assert "未取込チェック対象: 3件" in output
    assert "自動取込できた: 1件" in output
    assert "tool_run_id=1111111" in output
    assert "取込エラー: 1件" in output
    assert "tool_run_id=3333333" in output
    assert "boom" in output
    assert "まだ未検出: 1件" in output
    assert "tool_run_id=2222222" in output
