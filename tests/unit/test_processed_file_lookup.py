"""tool_intake.processed_file_lookupの単体テスト（DB非依存、ファイルシステムのみ）。"""
from tool_intake.processed_file_lookup import find_tool_output_files, is_plausible_tool_id


def test_is_plausible_tool_id_accepts_real_looking_id():
    assert is_plausible_tool_id("6546082") is True


def test_is_plausible_tool_id_rejects_short_placeholder():
    assert is_plausible_tool_id("-") is False


def test_find_tool_output_files_returns_empty_list_for_implausible_tool_id(tmp_path):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    (jp_dir / "some-file_with-dashes.csv").write_text("x")

    result = find_tool_output_files("JP", "-", jp_dir, tmp_path / "ex_primary_check")
    assert result == []


def test_find_tool_output_files_returns_empty_list_when_no_match(tmp_path):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    (jp_dir / "9999999_someone@example.com.csv").write_text("x")

    result = find_tool_output_files("JP", "1234567", jp_dir, tmp_path / "ex_primary_check")
    assert result == []


def test_find_tool_output_files_returns_single_match(tmp_path):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    expected = jp_dir / "1234567_someone@example.com_2026-01-01 00_00_00.csv"
    expected.write_text("x")
    (jp_dir / "9999999_other@example.com.csv").write_text("x")  # 別tool_run_id、無関係

    result = find_tool_output_files("JP", "1234567", jp_dir, tmp_path / "ex_primary_check")
    assert result == [expected]


def test_find_tool_output_files_returns_multiple_matches_sorted(tmp_path):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    older = jp_dir / "1234567_a@example.com_2026-01-01 00_00_00.csv"
    newer = jp_dir / "1234567_b@example.com_2026-02-01 00_00_00.csv"
    newer.write_text("x")
    older.write_text("x")

    result = find_tool_output_files("JP", "1234567", jp_dir, tmp_path / "ex_primary_check")
    assert result == sorted([older, newer])


def test_find_tool_output_files_does_not_match_prefix_without_underscore(tmp_path):
    """'1234567test.csv'のようにアンダースコアが無いファイル名には一致しないこと
    （実データに'6547026test_...'のような紛らわしい旧ファイルが存在するため）。"""
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    (jp_dir / "1234567test_something.csv").write_text("x")

    result = find_tool_output_files("JP", "1234567", jp_dir, tmp_path / "ex_primary_check")
    assert result == []


def test_find_tool_output_files_uses_jp_dir_for_jp_and_us_dir_for_us(tmp_path):
    jp_dir = tmp_path / "primary_check"
    us_dir = tmp_path / "ex_primary_check"
    jp_dir.mkdir()
    us_dir.mkdir()
    jp_file = jp_dir / "1234567_a@example.com.csv"
    us_file = us_dir / "1234567_a@example.com.csv"
    jp_file.write_text("x")
    us_file.write_text("x")

    assert find_tool_output_files("JP", "1234567", jp_dir, us_dir) == [jp_file]
    assert find_tool_output_files("US", "1234567", jp_dir, us_dir) == [us_file]


def test_find_tool_output_files_missing_directory_returns_empty(tmp_path):
    result = find_tool_output_files("JP", "1234567", tmp_path / "does_not_exist", tmp_path / "also_missing")
    assert result == []
