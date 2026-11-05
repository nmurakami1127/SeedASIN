"""レーンが「既存の出典の取得候補」を選ぶときに使うレポート（import）の集合（docs/DEVIATIONS.md #100）。

- 既存の出典の取得候補（plan・投入計画の方向判定・バッファ回復・plan-compare・outcome）: `lane_origin_imports(ln)` = [origin_import] + extra_origin_imports。
- 起点ASINの発見（select_origins / run_origin_discovery）・cross（select_cross_asins）・出品実績による起点の優先: **`ln.origin_import`だけ**
  （過去のimportを自動では混ぜない方針を維持。ここに extra を渡さない）。
"""
from __future__ import annotations

import copy

from acquisition.plan import PlanError, validate_origin_imports
from workflow.config import OPPOSITE, ConfigError, WorkflowConfig


def lane_origin_imports(ln) -> list[int]:
    """origin_importを先頭に、extraを後ろに並べた重複なしのimport_id（先頭が従来の候補）。"""
    out = [ln.origin_import]
    for x in getattr(ln, "extra_origin_imports", None) or []:
        if x not in out:
            out.append(x)
    return out


def has_lane_path(paths, direction: str, import_ids) -> bool:
    """投入項目の経路（dict）に、その方向でレーンのいずれかのimport由来のものがあるか（IMPORTの出典がEXPORTとして投入されない）。"""
    ids = set(import_ids)
    return any(p.get("direction") == direction and p.get("origin_import_id") in ids for p in paths)


def validate_lane_imports(reports, lane: str, ln) -> list:
    """レーンの全importを検証する（先頭=origin_import）。
    origin_import: ACTIVE・行エラー0・市場=lane。extra: ACTIVE・行エラー0・市場=反対側（同じ市場や不明なimportは拒否）。
    違反は PlanError（そのレーンだけが止まる。もう一方の市場のレーンは別のworkflow・別の検証なので影響しない）。"""
    info = validate_origin_imports(reports, [ln.origin_import])[0]
    if info.marketplace != lane:
        raise PlanError(f"{lane}: import_id={ln.origin_import} の市場は{info.marketplace}です（市場ごとに明示したレポートを指定してください）")
    infos = [info]
    for x in getattr(ln, "extra_origin_imports", None) or []:
        xi = validate_origin_imports(reports, [x])[0]
        if xi.marketplace != OPPOSITE[lane]:
            raise PlanError(f"{lane}: extra_origin_imports の import_id={x} の市場は{xi.marketplace}です"
                            f"（{lane}レーンのextraには{OPPOSITE[lane]}市場のレポートだけ指定できます）")
        infos.append(xi)
    if len(infos) > 1:
        validate_origin_imports(reports, [i.import_id for i in infos])  # 同じ市場の重複などの最終確認
    return infos


def with_extra_override(cfg: WorkflowConfig, extra: dict[str, list[int]]) -> WorkflowConfig:
    """設定ファイルを変えずに、メモリ上でextra_origin_importsを上書きしたコピーを返す（plan-compare用）。検証は通常の設定と同じ。"""
    c = copy.deepcopy(cfg)
    for lane, ids in extra.items():
        if lane not in c.lanes:
            raise ConfigError(f"--extra-origin-imports の市場 {lane} は設定の lanes にありません（{sorted(c.lanes)}）")
        c.lanes[lane].extra_origin_imports = list(ids)
    return c.validate()


def parse_extra_arg(text: str) -> dict[str, list[int]]:
    """`US=2,JP=1`（同じ市場を繰り返すと追加。`US=` で空にする）→ {"US": [2], "JP": [1]}。"""
    out: dict[str, list[int]] = {}
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        if "=" not in part:
            raise ConfigError(f"--extra-origin-imports は US=2,JP=1 の形式で指定してください: {part!r}")
        m, v = part.split("=", 1)
        m = m.strip().upper()
        ids = out.setdefault(m, [])
        if v.strip():
            try:
                ids.append(int(v))
            except ValueError as exc:
                raise ConfigError(f"import_id は整数で指定してください: {part!r}") from exc
    return out
