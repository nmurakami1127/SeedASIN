"""tool_intake.cli.resolve_tool_output_file()の単体テスト（DB非依存、ファイルシステムのみ）。

--fileの自動検出ロジック（docs/DEVIATIONS.md #37参照）。実際のimport-tool-output全体の
結合テストはtests/integration/test_tool_intake.py参照。
"""
import pytest

from tool_intake.cli import resolve_tool_output_file


def test_explicit_file_always_wins_over_auto_detection(monkeypatch):
    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("find_tool_output_files should not be called when --file is given")

    monkeypatch.setattr("tool_intake.cli.find_tool_output_files", _should_not_be_called)

    result = resolve_tool_output_file("US", "1234567", "explicit.csv")
    assert str(result) == "explicit.csv"


def test_auto_detects_single_match(monkeypatch, tmp_path, capsys):
    expected = tmp_path / "1234567_a@example.com.csv"
    monkeypatch.setattr("tool_intake.cli.find_tool_output_files", lambda mp, rid: [expected])

    result = resolve_tool_output_file("US", "1234567", None)
    assert result == expected
    assert "自動検出しました" in capsys.readouterr().out


def test_raises_system_exit_when_no_file_found(monkeypatch):
    monkeypatch.setattr("tool_intake.cli.find_tool_output_files", lambda mp, rid: [])

    with pytest.raises(SystemExit, match="自動検出できませんでした"):
        resolve_tool_output_file("US", "1234567", None)


def test_raises_system_exit_with_listing_when_multiple_files_found(monkeypatch, tmp_path):
    candidates = [tmp_path / "1234567_a@example.com.csv", tmp_path / "1234567_b@example.com.csv"]
    monkeypatch.setattr("tool_intake.cli.find_tool_output_files", lambda mp, rid: candidates)

    with pytest.raises(SystemExit) as exc_info:
        resolve_tool_output_file("US", "1234567", None)
    message = str(exc_info.value)
    assert "複数のファイルが見つかりました" in message
    assert str(candidates[0]) in message
    assert str(candidates[1]) in message
