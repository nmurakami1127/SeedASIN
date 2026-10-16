"""ビジネスレポートCSVの市場判定（置き場のフォルダで決める）。

市場は**解決済みの実パス**だけで決める。ファイル名・タイトル列・通貨・列構成は一切使わない
（ユーザー確定、docs/DEVIATIONS.md #56）:

- <reports_root>/US/ 配下 → US・輸出
- <reports_root>/JP/ 配下 → JP・輸入
- 所定フォルダ外 → --marketplaceの明示指定が必須（market_basis='EXPLICIT'）
- 所定フォルダ配下で--marketplaceがフォルダと矛盾 → 停止

統合ツールログ（tool_intake.log_ingest）のメモによる「輸出→US/輸入→JP」判定とは別の入力で、
そちらは変更しない。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "data" / "business_reports"

MARKETPLACES = ("US", "JP")
DIRECTION_BY_MARKETPLACE = {"US": "EXPORT", "JP": "IMPORT"}


class MarketResolutionError(Exception):
    """市場を確定できない、またはフォルダと指定が矛盾する場合に送出する（取込は行わない）。"""


@dataclass(frozen=True)
class MarketResolution:
    marketplace: str
    business_direction: str
    market_basis: str  # 'FOLDER' / 'EXPLICIT'
    resolved_path: Path
    stored_path: str  # DB保存用（プロジェクトルート配下なら相対パス）


def _norm(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_inside(path: Path, base: Path) -> bool:
    return _norm(path).startswith(_norm(base) + os.sep)


def _market_dirs(reports_root: Path) -> dict[str, Path]:
    root = reports_root.resolve()
    dirs = {mk: (root / mk).resolve() for mk in MARKETPLACES}
    us, jp = dirs["US"], dirs["JP"]
    if _norm(us) == _norm(jp) or _is_inside(us, jp) or _is_inside(jp, us):
        raise MarketResolutionError(
            f"USフォルダとJPフォルダが同一または入れ子の場所を指しています: US={us} JP={jp}"
        )
    return dirs


def folder_market(path: Path, reports_root: Path | None = None) -> str | None:
    """pathが所定のUS/JPフォルダ配下ならその市場、そうでなければNone。"""
    resolved = path.resolve()
    for mk, base in _market_dirs(reports_root or DEFAULT_REPORTS_ROOT).items():
        if _is_inside(resolved, base):
            return mk
    return None


def resolve_market(
    file: str | Path,
    explicit_marketplace: str | None = None,
    reports_root: Path | None = None,
    project_root: Path | None = None,
) -> MarketResolution:
    if explicit_marketplace is not None and explicit_marketplace not in MARKETPLACES:
        raise MarketResolutionError(f"--marketplaceはUSまたはJPを指定してください: {explicit_marketplace!r}")

    path = Path(file)
    if not path.exists() or not path.is_file():
        raise MarketResolutionError(f"ファイルが存在しません: {path}")
    resolved = path.resolve()

    folder_mk = folder_market(resolved, reports_root)
    if folder_mk is not None:
        if explicit_marketplace is not None and explicit_marketplace != folder_mk:
            raise MarketResolutionError(
                f"置き場のフォルダ({folder_mk})と--marketplace={explicit_marketplace}が矛盾しています。"
                f"取込を中止しました: {resolved}"
            )
        marketplace, basis = folder_mk, "FOLDER"
    else:
        if explicit_marketplace is None:
            raise MarketResolutionError(
                "所定フォルダ(data/business_reports/US または JP)の外にあるファイルは、"
                f"--marketplace US|JP の明示指定が必要です: {resolved}"
            )
        marketplace, basis = explicit_marketplace, "EXPLICIT"

    base_root = (project_root or PROJECT_ROOT).resolve()
    stored = str(resolved.relative_to(base_root)) if _is_inside(resolved, base_root) else str(resolved)
    return MarketResolution(
        marketplace=marketplace,
        business_direction=DIRECTION_BY_MARKETPLACE[marketplace],
        market_basis=basis,
        resolved_path=resolved,
        stored_path=stored,
    )
