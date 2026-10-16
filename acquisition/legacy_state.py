"""acquisition_legacy_evidence（追記専用）の観測行と判断行を畳み込んで、証拠ごとの現在状態を得る。

判断（CLASSIFY/CONFIRM/DISMISS/REOPEN）は行として追記されるだけで、状態を上書きしない。
現在の状態は evidence_id の昇順に畳み込んだ結果。履歴（誰が・いつ・なぜ）は行として全て残る。

取得先市場の状態（区画ごと。区画=その行を保持する市場）:
- UNKNOWN          取得先市場が未確定。出典の取得間隔の間、REVIEW_LEGACYとして両市場で自動選択しない
- CONFIRMED_THIS   この区画の市場で取得されたと人が確定。取得間隔の判定に「旧証拠」として数える
- CONFIRMED_OTHER  別の市場で取得されたと確定。この区画では無視
- DISMISSED        取得の証拠ではないと人が判断。無視
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass
class LegacyGroupState:
    evidence_group: str
    partition: str
    evidence_kind: str
    evidence_ref: str
    data_marketplace: str
    evidence_at: datetime
    evidence_at_basis: str | None
    asin_count: int | None
    raw_value: str
    success_grade: str
    market_grade: str | None
    type_grade: str | None
    market_hint: str | None
    type_hint: str | None
    source_type: str | None = None
    key_primary: str | None = None
    key_qualifier: str | None = None
    key_hash: str | None = None
    market_state: str = "UNKNOWN"
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def classified(self) -> bool:
        return self.source_type is not None and self.key_hash is not None


def fold_legacy(rows: Iterable[Any], partition: str) -> list[LegacyGroupState]:
    """rowsはRow（または同じキーを持つマッピング）。evidence_idで昇順に並べ替えてから畳み込む。"""
    states: dict[str, LegacyGroupState] = {}

    def _mapping(r: Any):
        return r._mapping if hasattr(r, "_mapping") else r

    # 並び順は**evidence_id（一意の連番）だけ**で決める。recorded_atは同一トランザクション内や同時刻で
    # 同値になり得るため順序の根拠にしない。入力の順序にも依存しない（呼び出し側が並べ替え忘れても同じ結果）。
    for r in sorted(rows, key=lambda x: _mapping(x)["evidence_id"]):
        m = _mapping(r)
        group = str(m["evidence_group"])
        if m["row_type"] == "OBSERVATION":
            states[group] = LegacyGroupState(
                evidence_group=group,
                partition=partition,
                evidence_kind=m["evidence_kind"],
                evidence_ref=m["evidence_ref"],
                data_marketplace=m["data_marketplace"],
                evidence_at=m["evidence_at"],
                evidence_at_basis=m["evidence_at_basis"],
                asin_count=m["asin_count"],
                raw_value=m["raw_value"],
                success_grade=m["success_grade"],
                market_grade=m["market_grade"],
                type_grade=m["type_grade"],
                market_hint=m["market_hint"],
                type_hint=m["type_hint"],
            )
            continue
        state = states.get(group)
        if state is None:
            continue  # 観測の無い判断行は無視
        d = m["decision_type"]
        state.decisions.append(
            {
                "decision_type": d,
                "target_marketplace": m["target_marketplace"],
                "source_type": m["source_type"],
                "reason": m["reason"],
                "actor": m["actor"],
                "recorded_at": m["recorded_at"],
            }
        )
        if d == "CLASSIFY":
            state.source_type, state.key_primary = m["source_type"], m["key_primary"]
            state.key_qualifier, state.key_hash = m["key_qualifier"], m["key_hash"]
        elif d == "CONFIRM":
            state.market_state = "CONFIRMED_THIS" if m["target_marketplace"] == partition else "CONFIRMED_OTHER"
        elif d == "DISMISS":
            state.market_state = "DISMISSED"
        elif d == "REOPEN":
            state.market_state = "UNKNOWN"
    return list(states.values())
