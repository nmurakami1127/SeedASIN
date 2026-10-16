"""scripts.import_legacy_tool_outputの純粋なロジック（DB非依存）の単体テスト。

dry_run=Trueの経路はsession_factoryに一切アクセスしないため、Noneを渡してDB無しで
検証できる。実際のファイル取込・DB書き込みの検証はtests/integration/
test_import_legacy_tool_output.py参照。
"""
from scripts.import_legacy_tool_output import ImportSummary, import_legacy_tool_output


def _make_tool_file(directory, tool_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{tool_id}_someone@example.com_2026-01-01 00_00_00.csv").write_text("x")


def test_import_summary_format_includes_all_fields():
    summary = ImportSummary(files_processed=10, asins_imported=500, files_not_found=3, parse_errors=2)
    output = summary.format()
    assert "取込成功件数（tool_output_asinへ新規追加したASIN行数）: 500件" in output
    assert "処理ファイル数: 10件" in output
    assert "ファイル未検出件数: 3件" in output
    assert "行単位の解析エラー件数（ASIN空欄・日付形式不正等）: 2件" in output


def test_dry_run_counts_found_files_without_touching_db(tmp_path):
    jp_dir = tmp_path / "primary_check"
    us_dir = tmp_path / "ex_primary_check"
    _make_tool_file(jp_dir, "1234567")
    _make_tool_file(us_dir, "7654321")

    links = [("JP", "1234567"), ("US", "7654321")]
    summary = import_legacy_tool_output(None, links, jp_dir, us_dir, dry_run=True)

    assert summary.files_processed == 2
    assert summary.files_not_found == 0
    assert summary.asins_imported == 0  # dry-runでは実際のINSERTは行わない


def test_dry_run_counts_unfindable_tool_id_as_not_found(tmp_path):
    jp_dir = tmp_path / "primary_check"
    us_dir = tmp_path / "ex_primary_check"

    links = [("JP", "9999999")]
    summary = import_legacy_tool_output(None, links, jp_dir, us_dir, dry_run=True)

    assert summary.files_not_found == 1
    assert summary.files_processed == 0


def test_dry_run_short_circuits_implausible_tool_id_without_filesystem_access(tmp_path):
    jp_dir = tmp_path / "primary_check"
    jp_dir.mkdir()
    (jp_dir / "some-file_with-dashes.csv").write_text("x")  # '-'を含む実在ファイル

    links = [("JP", "-")]
    summary = import_legacy_tool_output(None, links, jp_dir, tmp_path / "ex_primary_check", dry_run=True)

    assert summary.files_not_found == 1  # '-'に誤って一致しないこと
