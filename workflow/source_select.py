"""既存の出典の選択（docs/DEVIATIONS.md #98）。planの順位を土台に、効率による**後ろ回し**を行う。除外ではない。

`asin_collector.source_runner.select_targets`（従来の選び方）の結果を基準に、次を守る:
- 後退中（再評価日前）の出典は**順位を下げるだけ**で、選択の枠は減らさない（通常の出典がlimitに満たなければ、後退中の出典が枠を埋める）。
- 通常の出典（選択可能）が必要数(`needed`)に満たなければ、後退中の出典を**戻す**（再評価日を待たない。`released`）。
- 未評価（効率の標本が無い）出典のために1方向あたり`reserve`枠を確保する（高順位の既知の出典で埋まらないようにする）。
- 選ぶ数・対象の範囲（起点の経路を持つ出典・注文点数0を含む・1方向あたり上限）は`select_targets`と同じ。
planとprogress台帳だけを使い、DB・ブラウザには触らない（読み取りのみ）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from asin_collector.source_runner import MAX_SOURCES_HARD_CAP, select_targets

SECTIONS = ("EXPORT", "IMPORT")


@dataclass
class Selection:
    picks: list                                   # 取得する順（通常 → 後退中。方向ごとに交互ではなく、通常のEXPORT, 通常のIMPORT, 後退中のEXPORT, 後退中のIMPORT）
    per_section: dict = field(default_factory=dict)  # 方向ごとの {candidates, selectable, deprioritized, unevaluated, picked, reserved_in, released}
    scarce: bool = False
    selectable: int = 0                           # 方向をまたいで重複を除いた「選択可能（後退中でない）」出典数
    deprioritized: int = 0
    needed: int = 0


def _candidates(plan, section: str) -> list:
    """select_targetsと同じ範囲の候補を、上限なしの順位順で返す（起点の経路を持つ出典・注文点数0を含む）。"""
    out, seen = [], set()
    for e in plan.sections.get(section, []):
        if e.rep is None or e.facts.source_id in seen:
            continue
        seen.add(e.facts.source_id)
        out.append(e)
    return out


def baseline(plan, limit: int = MAX_SOURCES_HARD_CAP) -> list:
    """従来の選び方（効率を見ない）。比較の基準。"""
    out, seen = [], set()
    for sec in SECTIONS:
        for e in select_targets(plan, sec, limit, include_zero_units=True):
            if e.facts.source_id not in seen:
                seen.add(e.facts.source_id)
                out.append(e)
    return out


def choose(plan, *, slow: dict[int, str] | set[int] = frozenset(), evaluated: set[int] = frozenset(), reserve: int = 0, needed: int = 0,
           limit: int = MAX_SOURCES_HARD_CAP) -> Selection:
    """slow = 後退中の出典 {source_id: 再評価日}（setでも可）、evaluated = 効率の標本がある出典。"""
    slow_ids = set(slow)
    reeval = dict(slow) if isinstance(slow, dict) else {}
    cands = {sec: _candidates(plan, sec) for sec in SECTIONS}
    uniq = {e.facts.source_id for es in cands.values() for e in es}
    selectable = len(uniq - slow_ids)
    scarce = selectable < needed
    released = set(slow_ids) if scarce else set()
    sel = Selection(picks=[], scarce=scarce, selectable=selectable, deprioritized=len(uniq & slow_ids), needed=needed)
    normal_picks, low_picks, seen = [], [], set()
    for sec in SECTIONS:
        es = cands[sec]
        normal = [e for e in es if e.facts.source_id not in slow_ids or e.facts.source_id in released]
        low = sorted((e for e in es if e.facts.source_id in slow_ids and e.facts.source_id not in released),
                     key=lambda e: (reeval.get(e.facts.source_id) or "", es.index(e)))  # 再評価日が近い順
        order = normal + low
        chosen = order[:limit]
        # 未評価の出典のための枠: 選ばれた中の未評価が reserve に満たなければ、末尾の既知の出典と入れ替える
        unevaluated = [e for e in es if e.facts.source_id not in evaluated and e.facts.source_id not in slow_ids]
        have = [e for e in chosen if e in unevaluated]
        want = min(reserve, len(unevaluated))
        reserved_in = 0
        if len(have) < want:
            extra = [e for e in unevaluated if e not in chosen][: want - len(have)]
            drop = [e for e in reversed(chosen) if e not in unevaluated][: len(extra)]
            for d, x in zip(drop, extra):
                chosen[chosen.index(d)] = x
                reserved_in += 1
        chosen_set = {id(e) for e in chosen}
        chosen = [e for e in order if id(e) in chosen_set]  # 順位順に戻す
        sel.per_section[sec] = {"candidates": len(es), "selectable": len([e for e in es if e.facts.source_id not in slow_ids]),
                                "deprioritized": len([e for e in es if e.facts.source_id in slow_ids]), "unevaluated": len(unevaluated),
                                "picked": len(chosen), "reserved_in": reserved_in,
                                "released": len([e for e in chosen if e.facts.source_id in released])}
        for e in chosen:
            if e.facts.source_id in seen:
                continue
            seen.add(e.facts.source_id)
            (low_picks if e.facts.source_id in slow_ids and e.facts.source_id not in released else normal_picks).append(e)
    sel.picks = normal_picks + low_picks
    return sel


def engine_selection(plan, progress, market: str, cfg) -> Selection:
    """エンジンが取得対象の既存の出典を選ぶ関数。`Engine._existing_sources`・`plan-compare`・`outcome --config`が**この同じ関数**を使う
    （候補の集合が食い違わないように）。候補は`select_targets`と同じ条件: 起点の経路を持つ出典（注文点数0の起点だけの出典も含む=include_zero_units）、
    1方向あたり最大20件。plan.sections（その時点で取得できる出典）に入らない出典（期間内・実行中・保留・除外）は候補にならない。"""
    return choose(plan, slow=progress.slow_map(market), evaluated=progress.evaluated(market), reserve=cfg.completion.reserve_unevaluated,
                  needed=cfg.needed_sources())


def diagnose(plan) -> dict:
    """候補が少ない/0のときの理由の説明（読み取りのみ）: 方向ごとの出典数・起点の経路が無い数・注文点数0の数、取得できない出典（plan.blocked）の理由別の数。"""
    sec = {}
    for name in SECTIONS + ("NO_ORIGIN",):
        es = plan.sections.get(name, [])
        sec[name] = {"entries": len(es), "no_origin_path": len([e for e in es if e.rep is None]),
                     "zero_units": len([e for e in es if e.rep is not None and e.rep.units <= 0])}
    blocked: dict[str, int] = {}
    for _f, d in getattr(plan, "blocked", []) or []:
        codes = sorted({getattr(b, "code", str(b)) for b in (d.blockers or [])}) or ["(理由なし)"]
        for c in codes:
            blocked[c] = blocked.get(c, 0) + 1
    return {"sections": sec, "blocked_total": len(getattr(plan, "blocked", []) or []), "blocked_by_code": dict(sorted(blocked.items())),
            "needs_reaping": len(getattr(plan, "needs_reaping", []) or [])}


def format_diagnose(market: str, d: dict) -> list[str]:
    out = [f"  [{market}] 計画の内訳: " + " / ".join(f"{k} {v['entries']}件(起点経路なし{v['no_origin_path']}・注文点数0 {v['zero_units']})" for k, v in d["sections"].items())]
    out.append(f"  取得できない出典(期間内・実行中・保留・除外など。候補に入らない): {d['blocked_total']}件"
               + (" 内訳 " + ", ".join(f"{k}={n}" for k, n in d["blocked_by_code"].items()) if d["blocked_by_code"] else "")
               + (f" / 回収待ち{d['needs_reaping']}件" if d["needs_reaping"] else ""))
    return out


def selectable_counts(plan, slow, evaluated, *, needed: int = 0, reserve: int = 0) -> dict:
    """方向ごと・合計の、選択可能な出典数（outcome・plan-compareの表示用）。"""
    s = choose(plan, slow=slow, evaluated=evaluated, needed=needed, reserve=reserve)
    return {"sections": s.per_section, "selectable": s.selectable, "deprioritized": s.deprioritized, "needed": needed, "scarce": s.scarce}


def compare(plan, slow, evaluated, *, needed: int = 0, reserve: int = 0) -> dict:
    """（読み取りのみ）従来の選び方と新しい選び方の取得対象を比べる。"""
    before = [e.facts.source_id for e in baseline(plan)]
    sel = choose(plan, slow=slow, evaluated=evaluated, needed=needed, reserve=reserve)
    after = [e.facts.source_id for e in sel.picks]
    pos_b = {sid: i for i, sid in enumerate(before)}
    pos_a = {sid: i for i, sid in enumerate(after)}
    moved_down = [sid for sid in before if sid in pos_a and sid in set(slow) and pos_a[sid] > pos_b[sid]]
    return {"before": before, "after": after, "same_count": len(before) == len(after), "dropped": [s for s in before if s not in pos_a],
            "added": [s for s in after if s not in pos_b], "moved_down": moved_down, "selection": sel,
            "released": [s for s in after if s in set(slow) and sel.scarce]}


def format_compare(market: str, cmp: dict, slow, simulated_needed: int | None = None) -> list[str]:
    sel: Selection = cmp["selection"]
    out = [f"== {market} 取得対象の比較（従来 → 新） ==",
           f"選択可能な出典: {sel.selectable}件（後退中{sel.deprioritized}件 / 必要数{sel.needed}件{' → 不足のため後退中を戻す' if sel.scarce else ''}）"]
    for sec, d in sel.per_section.items():
        out.append(f"  {sec}: 候補{d['candidates']} 選択可能{d['selectable']} 後退中{d['deprioritized']} 未評価{d['unevaluated']} → 取得対象{d['picked']}"
                   f"（未評価枠で追加{d['reserved_in']} / 戻した{d['released']}）")
    tag = f"（--simulate-shortage {simulated_needed} による模擬。必要数を{simulated_needed}件として判定）" if simulated_needed is not None else ""
    if sel.scarce:
        back = [f"#{sid}(再評価日 {slow[sid] if isinstance(slow, dict) else '-'})" for sid in cmp["released"]]
        out.append(f"補充: 選択可能{sel.selectable}件 < 必要{sel.needed}件 → 後退中の出典{len(back)}件を再評価日を待たずに戻した{tag}: " + (" ".join(back) or "なし（後退中の出典が無い）"))
    else:
        out.append(f"補充: 不要（選択可能{sel.selectable}件 >= 必要{sel.needed}件。後退中の出典は戻していない）{tag}")
    out.append(f"従来 {len(cmp['before'])}件 → 新 {len(cmp['after'])}件（件数{'同じ' if cmp['same_count'] else '違う'}）")
    out.append(f"  後ろへ回した: {cmp['moved_down'] or 'なし'}")
    out.append(f"  対象から外れた: {cmp['dropped'] or 'なし'}  対象に加わった: {cmp['added'] or 'なし'}")
    out.append("  従来: " + " ".join(f"#{s}" for s in cmp["before"]))
    out.append("  新  : " + " ".join(f"#{s}" + ("*" if s in set(slow) else "") for s in cmp["after"]) + "   （*=後退中）")
    return out
