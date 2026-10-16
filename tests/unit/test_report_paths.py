"""origin_selection.paths（置き場フォルダによる市場判定）の単体テスト。

市場はファイル名・タイトル列・通貨ではなく、解決済みパスだけで決まる。
"""
from pathlib import Path

import pytest

from origin_selection.paths import MarketResolutionError, folder_market, resolve_market


@pytest.fixture()
def roots(tmp_path: Path):
    project = tmp_path / "proj"
    reports = project / "data" / "business_reports"
    (reports / "US").mkdir(parents=True)
    (reports / "JP").mkdir(parents=True)
    return project, reports


def _write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_us_folder_resolves_to_us_export(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    r = resolve_market(f, None, reports, project)
    assert (r.marketplace, r.business_direction, r.market_basis) == ("US", "EXPORT", "FOLDER")


def test_jp_folder_resolves_to_jp_import(roots):
    project, reports = roots
    f = _write(reports / "JP" / "report.csv")
    r = resolve_market(f, None, reports, project)
    assert (r.marketplace, r.business_direction, r.market_basis) == ("JP", "IMPORT", "FOLDER")


def test_subfolder_is_judged_by_first_level_folder(roots):
    project, reports = roots
    f = _write(reports / "JP" / "archive" / "2026" / "report.csv")
    assert resolve_market(f, None, reports, project).marketplace == "JP"


def test_stored_path_is_relative_to_project_root(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    r = resolve_market(f, None, reports, project)
    assert Path(r.stored_path) == Path("data") / "business_reports" / "US" / "report.csv"


def test_filename_does_not_decide_market(roots):
    """JP-...という名前のファイルでもUSフォルダに置かれていればUS。"""
    project, reports = roots
    f = _write(reports / "US" / "JP-BusinessReport-16-10-26.csv")
    assert resolve_market(f, None, reports, project).marketplace == "US"


def test_content_does_not_decide_market(roots):
    """JP形式（タイトル列・円）の内容でもフォルダが市場を決める。"""
    project, reports = roots
    f = _write(reports / "US" / "x.csv", "（子）ASIN,タイトル,注文商品の売上額\nB0TESTJP01,テスト,￥100\n")
    assert resolve_market(f, None, reports, project).marketplace == "US"


def test_matching_explicit_marketplace_is_accepted(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    r = resolve_market(f, "US", reports, project)
    assert (r.marketplace, r.market_basis) == ("US", "FOLDER")


def test_conflicting_explicit_marketplace_stops(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    with pytest.raises(MarketResolutionError, match="矛盾"):
        resolve_market(f, "JP", reports, project)


def test_outside_folder_requires_explicit_marketplace(roots, tmp_path: Path):
    project, reports = roots
    f = _write(tmp_path / "elsewhere" / "report.csv")
    with pytest.raises(MarketResolutionError, match="--marketplace"):
        resolve_market(f, None, reports, project)


def test_outside_folder_with_explicit_marketplace_is_explicit_basis(roots, tmp_path: Path):
    project, reports = roots
    f = _write(tmp_path / "elsewhere" / "report.csv")
    r = resolve_market(f, "JP", reports, project)
    assert (r.marketplace, r.business_direction, r.market_basis) == ("JP", "IMPORT", "EXPLICIT")
    assert Path(r.stored_path) == f.resolve()  # プロジェクト外は絶対パス


def test_reports_root_itself_is_outside(roots):
    """business_reports直下（US/JPの外）は所定外として扱う。"""
    project, reports = roots
    f = _write(reports / "report.csv")
    with pytest.raises(MarketResolutionError, match="--marketplace"):
        resolve_market(f, None, reports, project)


def test_dotdot_traversal_is_resolved_before_judging(roots):
    """US/../JP/report.csv は解決後のパス(JP)で判定される。"""
    project, reports = roots
    f = _write(reports / "JP" / "report.csv")
    tricky = reports / "US" / ".." / "JP" / "report.csv"
    assert resolve_market(tricky, None, reports, project).marketplace == "JP"


def test_case_differences_are_normalized(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    odd = Path(str(f).replace("business_reports", "BUSINESS_Reports"))
    if odd.exists():  # 大文字小文字を区別しないファイルシステム（Windows）のみ
        assert resolve_market(odd, None, reports, project).marketplace == "US"


def test_missing_file_stops(roots):
    project, reports = roots
    with pytest.raises(MarketResolutionError, match="存在しません"):
        resolve_market(reports / "US" / "nope.csv", None, reports, project)


def test_invalid_explicit_marketplace_stops(roots):
    project, reports = roots
    f = _write(reports / "US" / "report.csv")
    with pytest.raises(MarketResolutionError):
        resolve_market(f, "UK", reports, project)


def test_folder_market_helper(roots, tmp_path: Path):
    project, reports = roots
    assert folder_market(_write(reports / "JP" / "a.csv"), reports) == "JP"
    assert folder_market(_write(tmp_path / "other" / "a.csv"), reports) is None
