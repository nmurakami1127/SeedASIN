"""自動保留の自動解除の実行（docs/DEVIATIONS.md #86）。`python -m workflow.cli recovery-run`。

**既定ではドライランのみ**。DBを更新するのは、次の両方が揃ったときだけ:
  1. コマンドに `--apply` を付けた
  2. 設定 `recovery.auto_release.enabled` が true（既定 false）
どちらかが欠ければ、ドライラン（`recovery-plan`と同じ判定・同じ表示。読み取り専用の接続）で、DBを更新しない。
watchの自動実行ループ（workflow/watch.py）には**組み込まれていない**。人が実行したときだけ動く。Amazonにはアクセスしない。

解除の実体は`acquisition.registry.SourceRegistry.release`と同じ効果（status→ACTIVE、hold_reason/hold_detail/held_atをクリア、failure_streak_reset_atを現在時刻に）。
ただしregistryの関数は自前のトランザクションを開くため、「解除」と「AUTO_RELEASEの記録」を**同一トランザクション**にできない。そのため同等のSQLをここに持つ
（`acquisition/`は変更していない。ACTIVEのままCYCLE_RESTART_LIMITの出典は、registryと同じくfailure_streak_reset_atだけを更新する）。
`tests/integration/test_auto_release.py`が、registry.releaseと同じ行になることを確かめる。

1出典ごとの手順（1トランザクション。`SessionFactory.get_session(market)`なのでRLSでその市場の行だけ）:
  1. 識別ロック → `acquisition_source`行を`FOR UPDATE`（claim・手動の保留/解除と同じロック順序。watch・手動取得とは直列化される）
  2. **ロックの下で再判定**（ドライラン時点の判定を信用しない）: 自動保留のまま／leaseなし／意図的な保留・セラー側の記録でない／OPEN一巡は
     RESUME_OPENの条件を満たす／30日で2回の上限内／バーストでない／前回の自動解除後に同原因で再発していない／USのREPEATED_PAGEでない
  3. 候補のままなら、解除のUPDATEとAUTO_RELEASEイベントのINSERTを同じトランザクションで行う（どちらかが失敗すれば両方なし）
  4. 候補でなくなっていれば何もしない（二重解除しない。理由を報告）
1出典の失敗（例外）はその出典だけをERRORとして報告し、他の出典の処理は続ける。市場ごとに別の接続で処理し、一方の失敗は他方を止めない。

OPEN一巡の解除（RESUME_OPEN）は`acquisition_source`の列だけを変える。`collector_run`（一巡・再開位置next_url・next_page_no）と`collector_asin`には書き込まない
（一巡を閉じる・BROKENにする・位置を消す操作は存在しない）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text

from acquisition.locks import identity_lock
from workflow.config import WorkflowConfig
from workflow.hold_recovery import (AUTO_RELEASE_ACTOR, CANDIDATE, MarketPlan, Verdict, build_market_plan, evaluate, market_lines)

# 実行モード
DRYRUN, APPLY_DISABLED, APPLY = "DRYRUN", "APPLY_DISABLED", "APPLY"
MODE_NOTES = {
    DRYRUN: "ドライラン: --apply が無いため、何も解除しません（DBは更新しません）。",
    APPLY_DISABLED: "ドライラン: --apply が指定されましたが、設定 recovery.auto_release.enabled が false のため、何も解除しません（DBは更新しません）。",
    APPLY: "実行: --apply が指定され、recovery.auto_release.enabled=true のため、再判定に通った出典だけを解除します。",
}
RELEASED, SKIPPED, ERROR = "RELEASED", "SKIPPED", "ERROR"


def run_mode(apply: bool, cfg: WorkflowConfig) -> str:
    if not apply:
        return DRYRUN
    return APPLY if cfg.recovery.auto_release.enabled else APPLY_DISABLED


def effective_cap(cfg: WorkflowConfig, limit: int | None) -> int:
    """1回の実行で解除する最大件数 = recovery.auto_release.max_per_run と --limit の小さい方。"""
    cap = cfg.recovery.auto_release.max_per_run
    return cap if limit is None else min(cap, limit)


@dataclass
class ReleaseResult:
    source_id: int
    source_type: str = ""
    name: str = ""
    outcome: str = SKIPPED          # RELEASED / SKIPPED / ERROR
    reason: str = ""                # SKIPPEDなら再判定で外れた理由コード（NOT_FOUND / NOT_HELD / 対象外コード）
    detail: str = ""
    candidate_class: str | None = None
    event_id: int | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class MarketRun:
    market: str
    mode: str
    cap: int
    plan: MarketPlan | None = None
    planned_ids: tuple[int, ...] = ()
    results: list[ReleaseResult] = field(default_factory=list)
    error: str | None = None

    @property
    def released(self) -> int:
        return sum(1 for r in self.results if r.outcome == RELEASED)

    def to_dict(self) -> dict:
        return {"market": self.market, "mode": self.mode, "cap": self.cap, "error": self.error, "planned": list(self.planned_ids),
                "released": self.released, "results": [r.to_dict() for r in self.results],
                "plan": self.plan.to_dict() if self.plan else None}


# ---------------------------------------------------------------------- 1出典の解除（1トランザクション）

def auto_release_detail(v: Verdict, burst: dict, cfg: WorkflowConfig, prior_status: str, prior_hold_reason: str | None) -> str:
    """AUTO_RELEASEイベントのdetail（JSON文字列）。根拠: 原因コード・根拠run・一巡の状態・候補区分・バーストなしの確認・30日内の自動解除回数。"""
    i = v.info
    d = {
        "kind": "AUTO_RECOVERY",
        "summary": f"自動解除（{v.candidate_class}）: {prior_status}/{prior_hold_reason or '-'} → ACTIVE。原因={v.reason}",
        "cause_code": v.reason,
        "evidence_run_id": i.cause_run_id,
        "evidence_run_completed_at": i.cause_at.isoformat() if i.cause_at else None,
        "failure_codes_since_hold": list(i.failure_codes),
        "cycle_state": i.cycle_state,
        "cursor_present": i.cursor_present,
        "cycle_has_pages": i.cycle_has_pages,
        "asin_count": i.asin_count,
        "candidate_class": v.candidate_class,
        "burst_free": not (burst.get("codes") or {}),
        "burst_codes_checked": sorted((burst.get("codes") or {}).keys()),
        "burst_window_hours": burst.get("window_hours"),
        "burst_min_sources": burst.get("min_sources"),
        "auto_releases_in_window_before": i.recent_auto_releases,
        "auto_releases_in_window_after": i.recent_auto_releases + 1,
        "max_auto_releases": cfg.recovery.max_auto_releases,
        "window_days": cfg.recovery.window_days,
        "cooldown_hours": cfg.recovery.cooldown_hours,
        "prior_status": prior_status,
        "prior_hold_reason": prior_hold_reason,
    }
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _apply_release(conn, market: str, source_id: int, status: str) -> None:
    """解除のUPDATE。`SourceRegistry.release`と同じ効果（ON_HOLD→ACTIVEと保留項目のクリア＋failure_streak_reset_at。ACTIVE/CYCLE_RESTART_LIMITはreset_atのみ）。"""
    if status == "ACTIVE":
        conn.execute(text("UPDATE acquisition_source SET failure_streak_reset_at = clock_timestamp() WHERE source_id = :id"), {"id": source_id})
        return
    conn.execute(text("UPDATE acquisition_source SET status = 'ACTIVE', hold_reason = NULL, hold_detail = NULL, held_at = NULL, "
                      "failure_streak_reset_at = clock_timestamp() WHERE source_id = :id"), {"id": source_id})


def _insert_auto_release_event(conn, market: str, source_id: int, detail: str) -> int:
    return conn.execute(
        text("INSERT INTO acquisition_source_event (source_id, marketplace, event_type, detail, actor, occurred_at) "
             "VALUES (:id, :mp, 'AUTO_RELEASE', :d, :a, clock_timestamp()) RETURNING event_id"),
        {"id": source_id, "mp": market, "d": detail, "a": AUTO_RELEASE_ACTOR}).scalar_one()


def release_one(factory, market: str, source_id: int, cfg: WorkflowConfig) -> ReleaseResult:
    """1出典を、ロックの下で再判定したうえで解除する。解除とAUTO_RELEASEの記録は同じトランザクション。例外は呼び出し側（run_market）が出典単位で捕捉する。"""
    res = ReleaseResult(source_id)
    with factory.get_session(market) as conn, conn.begin():
        ident = conn.execute(text("SELECT source_type, key_hash FROM acquisition_source WHERE source_id = :id"), {"id": source_id}).one_or_none()
        if ident is None:
            res.reason, res.detail = "NOT_FOUND", f"source_id={source_id} は市場{market}に存在しない"
            return res
        identity_lock(conn, market, ident.source_type, ident.key_hash.strip())
        row = conn.execute(text("SELECT status, hold_reason FROM acquisition_source WHERE source_id = :id FOR UPDATE"), {"id": source_id}).one()
        ev = evaluate(conn, market, cfg, [source_id])
        v = ev.verdicts[0] if ev.verdicts else None
        res.source_type = ident.source_type
        if v is None:
            res.reason, res.detail = "NOT_HELD", "保留・上限で止まっていない（すでに解除済み。二重解除しない）"
            return res
        res.name, res.candidate_class = v.name, v.candidate_class
        if v.verdict != CANDIDATE:
            res.reason, res.detail = v.reason, v.detail
            return res
        detail = auto_release_detail(v, ev.burst, cfg, row.status, row.hold_reason)
        _apply_release(conn, market, source_id, row.status)
        res.event_id = _insert_auto_release_event(conn, market, source_id, detail)
        res.outcome = RELEASED
        res.reason = v.reason
    return res


# ---------------------------------------------------------------------- 市場ごとの実行

def run_market(factory, market: str, cfg: WorkflowConfig, *, apply: bool, limit: int | None = None) -> MarketRun:
    """1つの市場の復旧。ドライラン（DRYRUN/APPLY_DISABLED）はDBを更新しない。APPLYのときだけ、候補を古い失敗の順に上限件数まで解除する。"""
    mode = run_mode(apply, cfg)
    cap = effective_cap(cfg, limit)
    out = MarketRun(market, mode, cap)
    try:
        plan = build_market_plan(factory, market, cfg, limit=None)    # 読み取り専用の接続。全候補を見て、上限は実行側で適用する
        out.plan = plan
        cands = [v for v in plan.verdicts if v.verdict == CANDIDATE]
        out.planned_ids = tuple(v.source_id for v in cands[:cap])
        if mode != APPLY:
            return out
        for v in cands:
            if out.released >= cap:
                break
            try:
                r = release_one(factory, market, v.source_id, cfg)
            except Exception as exc:  # noqa: BLE001 - 1出典の失敗で他の出典を止めない。そのトランザクションは巻き戻っている
                r = ReleaseResult(v.source_id, v.source_type, v.name, ERROR, type(exc).__name__, str(exc)[:300], v.candidate_class)
            out.results.append(r)
    except Exception as exc:  # noqa: BLE001 - 一方の市場の失敗で他方を止めない
        out.error = f"{type(exc).__name__}: {exc}"[:300]
    return out


def run_markets(factory, markets, cfg: WorkflowConfig, *, apply: bool, limit: int | None = None) -> dict[str, MarketRun]:
    return {m: run_market(factory, m, cfg, apply=apply, limit=limit) for m in markets}


# ---------------------------------------------------------------------- 表示

def format_runs(runs: dict[str, MarketRun], *, limit: int | None = None) -> list[str]:
    modes = {r.mode for r in runs.values()}
    mode = APPLY if APPLY in modes else (APPLY_DISABLED if APPLY_DISABLED in modes else DRYRUN)
    lines = ["自動保留の自動解除（recovery-run）。" + MODE_NOTES[mode],
             "Amazonへのアクセスはありません。watchの自動実行には組み込まれていません（人が実行したときだけ動きます）。"]
    for m, r in runs.items():
        lines.append("")
        if r.error:
            lines.append(f"[{m}] 失敗: {r.error}")
            continue
        label = "解除予定" if r.mode == APPLY else "候補"
        lines.extend(market_lines(m, r.plan, planned_ids=frozenset(r.planned_ids), planned_label=label))
        lines.append(f"    今回の解除上限: {r.cap}件（recovery.auto_release.max_per_run と --limit の小さい方）／解除予定 {len(r.planned_ids)}件")
        if r.mode == APPLY:
            for x in r.results:
                if x.outcome == RELEASED:
                    lines.append(f"    解除しました #{x.source_id} {x.source_type} {x.name}（区分={x.candidate_class}・AUTO_RELEASE event_id={x.event_id}）")
                elif x.outcome == SKIPPED:
                    lines.append(f"    見送り #{x.source_id} {x.source_type} {x.name}: 解除直前の再判定で対象外（{x.reason}: {x.detail}）")
                else:
                    lines.append(f"    失敗 #{x.source_id} {x.source_type} {x.name}: {x.reason}: {x.detail}（この出典の変更は巻き戻し済み。他の出典は続行）")
            lines.append(f"    結果: 解除{r.released}件・見送り{sum(1 for x in r.results if x.outcome == SKIPPED)}件・失敗{sum(1 for x in r.results if x.outcome == ERROR)}件")
        else:
            lines.append("    （DBは更新していません）")
    return lines


def has_failures(runs: dict[str, MarketRun]) -> bool:
    return any(r.error or any(x.outcome == ERROR for x in r.results) for r in runs.values())
