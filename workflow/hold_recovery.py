"""自動保留の復旧候補の判定（ドライラン。**読み取り専用**。docs/DEVIATIONS.md #84）。

背景: 自動保留（AUTO_FAILURES / AUTO_CYCLE_RESTARTS / ACTIVEのままCYCLE_RESTART_LIMIT）された出典は、人が`release-hold`で解除するまで止まる。
原因が一時的（ネットワーク・Amazon側の障害）や既知の対策がある（価格帯分割）ものは、安全な条件を満たす場合に限り復旧できる。
今回はその**判定とドライラン表示だけ**を作る（P2）。解除の実行（P3）はしない。DBは一切更新せず、Amazonにもアクセスしない
（接続は`SET TRANSACTION READ ONLY`。更新しようとすればDBが拒否する）。

候補にする条件（全部満たす場合だけ。1つでも外れれば理由つきで対象外）:
1. 自動保留であること（意図的な保留・除外・セラー側の記録は対象外。判定は`workflow.stall.classify_hold`と共通）
2. 実行中のleaseが無いこと
3. OPENな一巡が無いこと（BROKENまたは一巡なし。OPEN・末尾到達・放置期限切れのOPENを閉じる／強制終了する提案は出さない）
4. 過去の自動復旧の回数が上限（既定: 30日で2回）に達していないこと
5. 原因別の条件:
   - REPEATED_PAGE: JP かつ SELLER かつ価格帯分割（`partition.price_band`）が有効なときだけ。USは価格帯分割が未検証なので常に対象外（US_PRICE_BAND_UNVERIFIED）。
   - PAGE_LOAD_FAILED: 環境障害のバースト中でなく、最後の失敗から冷却期間（既定24時間）が過ぎていること。
   - OVERLAP_MISMATCH・その他のerror_code: 要人手確認（対象外）。原因不明（失敗したrunが無い等）も対象外。

判定は純粋関数`judge()`（DBに触れない）と、データ収集`build_market_plan()`に分ける。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text

from acquisition.eligibility import db_now, decide, load_configs, load_facts
from workflow.config import WorkflowConfig
from workflow.stall import KIND_AUTO, KIND_INTENTIONAL, classify_hold, detect_error_bursts, load_error_burst_events

# ---- 判定結果
CANDIDATE, EXCLUDED = "CANDIDATE", "EXCLUDED"
OPEN_CYCLE_STATES = ("OPEN", "END_REACHED", "EXPIRED")   # collector_runのcycle_stateがOPENのままの一巡（resolve_cycleの状態名）

# 対象外の理由コード（表示名つき）。意図的な保留の理由は workflow.stall.classify_hold のラベルと同じ
REASON_LABELS = {
    "MANUAL_HOLD": "意図的な保留（hold_reason=MANUAL）",
    "MANUAL_EXCLUDED": "意図的な除外（EXCLUDED）",
    "SELLER_COMPETITOR_SAME_SERVICE": "セラー側の記録: 同じサービスの競合（COMPETITOR_SAME_SERVICE）",
    "SELLER_RETRY_LATER": "セラー側の記録: 後で再試行（RETRY_LATER）",
    "PERMANENTLY_EXCLUDED": "セラー側の記録: 今後も取得しない（PERMANENTLY_EXCLUDED）",
    "UNKNOWN_HOLD_REASON": "知らない保留理由（安全側で意図的として扱う）",
    "LEASE_ACTIVE": "実行中のleaseがある",
    "OPEN_CYCLE": "OPENな一巡がある（閉じる・強制終了する提案はしない）",
    "RECOVERY_LIMIT_REACHED": "過去の自動復旧の回数が上限に達している",
    "UNKNOWN_CAUSE": "原因不明（失敗したrunが無い）",
    "US_PRICE_BAND_UNVERIFIED": "USの価格帯分割は未検証",
    "PRICE_BAND_NOT_ENABLED": "価格帯分割が使えない（JPのSELLERで partition.price_band が有効な場合のみ）",
    "BURST_DEFERRED": "環境障害のバースト中のため見送り",
    "COOLDOWN_PENDING": "冷却期間中",
    "NEEDS_MANUAL_REVIEW": "要人手確認（原因別の自動復旧の対象外のerror_code）",
}
CANDIDATE_PROPOSALS = {
    "REPEATED_PAGE": "解除して価格帯分割で再取得",
    "PAGE_LOAD_FAILED": "解除して通常の取得を再試行（環境障害ではなく、冷却期間が過ぎている）",
}


# ---------------------------------------------------------------------- 純粋関数

def count_recent_auto_releases(events: list[tuple[str, str, datetime]], now: datetime, window_days: int) -> int:
    """過去`window_days`日以内（`now - window_days <= 発生時刻 <= now`）の自動復旧の回数。events=(event_type, actor, 発生時刻)。

    **P3で差し替える**: 自動解除を実装するときは、専用のイベント（AUTO_RELEASE）を数える。今は`acquisition_source_event.event_type`の制約に
    AUTO_RELEASEが無い（スキーマ変更をしない方針）ため、既存のRELEASEイベントのうち実行者が'system'（自動処理）のものだけを数える。
    人の手による解除（actorは利用者名）は数えない。現状'system'のRELEASEは作られない（0件）ので、P3まで常に0になる。
    """
    start = now - timedelta(days=window_days)
    return sum(1 for et, actor, at in events if et == "RELEASE" and actor == "system" and at is not None and start <= at <= now)


def auto_release_limit_reached(recent_count: int, max_auto_releases: int) -> bool:
    """過去の自動復旧がちょうど上限の回数なら、もう候補にしない（max_auto_releases=0なら常に候補にしない）。"""
    return recent_count >= max_auto_releases


@dataclass(frozen=True)
class SourceInfo:
    """1出典の判定に必要な事実（DBから集めた値。judgeはこれだけを見る）。"""
    source_id: int
    market: str                     # 取得先ページの市場
    source_type: str
    name: str
    status: str
    hold_reason: str | None
    blocked_reason: str | None      # storefront_blocked_seller（SELLERのみ）
    codes: tuple[str, ...]          # 取得の判定のblockerのコード
    cycle_state: str | None         # 最新の一巡の状態（resolve_cycle: OPEN/COMPLETE/BROKEN/EXPIRED/END_REACHED/RUNNING。一巡なしはNone）
    lease_active: bool
    cause_code: str | None          # 最後に失敗したrunのerror_code（無ければNone）
    cause_at: datetime | None       # その失敗の完了時刻
    asin_count: int = 0
    release_count: int = 0          # RELEASEイベントの総数（誰が解除したかを問わない）
    recent_auto_releases: int = 0
    last_run: dict | None = None


@dataclass(frozen=True)
class RecoveryContext:
    market: str
    now: datetime
    burst_codes: frozenset[str]
    cooldown_hours: int
    max_auto_releases: int
    price_band_enabled: bool        # この市場の取得先で partition.price_band が有効か


@dataclass
class Verdict:
    source_id: int
    source_type: str
    name: str
    kind: str                       # AUTO / INTENTIONAL
    verdict: str                    # CANDIDATE / EXCLUDED
    reason: str                     # 対象外の理由コード（候補なら原因のerror_code）
    detail: str = ""
    proposal: str | None = None
    eligible_at: datetime | None = None
    info: SourceInfo | None = field(default=None, repr=False)


def judge(info: SourceInfo, ctx: RecoveryContext) -> Verdict | None:
    """1出典を判定する。保留・除外・上限のどれでもない（classify_holdがNone）出典はNone。判定の順序は上のモジュール説明どおり（最初に外れた条件が理由になる）。"""
    kind, label = classify_hold(info.status, info.hold_reason, info.blocked_reason, info.codes)
    if kind is None:
        return None

    def out(reason: str, detail: str = "", **kw) -> Verdict:
        return Verdict(info.source_id, info.source_type, info.name, kind, EXCLUDED, reason, detail, info=info, **kw)

    if kind == KIND_INTENTIONAL:
        return out(label)
    if info.lease_active or info.cycle_state == "RUNNING":
        return out("LEASE_ACTIVE")
    if info.cycle_state in OPEN_CYCLE_STATES:
        return out("OPEN_CYCLE", f"一巡の状態={info.cycle_state}")
    if auto_release_limit_reached(info.recent_auto_releases, ctx.max_auto_releases):
        return out("RECOVERY_LIMIT_REACHED", f"過去の自動復旧{info.recent_auto_releases}回（上限{ctx.max_auto_releases}回）")
    code = info.cause_code
    if code is None:
        return out("UNKNOWN_CAUSE")
    if code == "REPEATED_PAGE":
        if ctx.market != "JP":
            return out("US_PRICE_BAND_UNVERIFIED", f"{ctx.market}の{info.source_type}は価格帯分割が未検証")
        if info.source_type != "SELLER" or not ctx.price_band_enabled:
            return out("PRICE_BAND_NOT_ENABLED", "partition.price_band がこの市場で有効ではない、またはSELLERではない")
        return Verdict(info.source_id, info.source_type, info.name, kind, CANDIDATE, code, proposal=CANDIDATE_PROPOSALS[code], info=info)
    if code == "PAGE_LOAD_FAILED":
        if code in ctx.burst_codes:
            return out("BURST_DEFERRED", "直近に複数の出典で同じ失敗が出ている（環境障害の疑い）")
        if info.cause_at is not None:
            eligible_at = info.cause_at + timedelta(hours=ctx.cooldown_hours)
            if ctx.now < eligible_at:
                return out("COOLDOWN_PENDING", f"最後の失敗 {info.cause_at:%Y-%m-%d %H:%M} UTC から{ctx.cooldown_hours}時間", eligible_at=eligible_at)
        return Verdict(info.source_id, info.source_type, info.name, kind, CANDIDATE, code, proposal=CANDIDATE_PROPOSALS[code], info=info)
    return out("NEEDS_MANUAL_REVIEW", f"error_code={code}")


# ---------------------------------------------------------------------- データ収集（読み取りのみ）

@contextmanager
def readonly_session(factory, market: str):
    """市場ごとのDB接続を、読み取り専用のトランザクションで開く。更新を試みるとDBが拒否する（SET TRANSACTION READ ONLY）。"""
    with factory.get_session(market) as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


@dataclass
class MarketPlan:
    market: str
    now: str | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    candidates_total: int = 0
    candidates_omitted_by_limit: int = 0
    burst: dict = field(default_factory=dict)
    error: str | None = None

    def summary(self) -> dict:
        auto = sum(1 for v in self.verdicts if v.kind == KIND_AUTO)
        excluded: dict[str, int] = {}
        for v in self.verdicts:
            if v.verdict == EXCLUDED:
                excluded[v.reason] = excluded.get(v.reason, 0) + 1
        return {"in_scope": len(self.verdicts), "auto_hold": auto, "intentional_hold": len(self.verdicts) - auto,
                "candidates": self.candidates_total, "candidates_listed": self.candidates_total - self.candidates_omitted_by_limit,
                "excluded": sum(excluded.values()), "excluded_by_reason": dict(sorted(excluded.items()))}

    def to_dict(self) -> dict:
        return {"market": self.market, "now": self.now, "dry_run": True, "error": self.error, "summary": self.summary(), "burst": self.burst,
                "sources": [verdict_dict(v) for v in self.verdicts]}


def _iso(x: datetime | None) -> str | None:
    return x.isoformat() if x else None


def verdict_dict(v: Verdict) -> dict:
    i = v.info
    d = {"source_id": v.source_id, "source_type": v.source_type, "name": v.name, "kind": v.kind, "verdict": v.verdict, "reason": v.reason,
         "reason_label": REASON_LABELS.get(v.reason, v.reason), "detail": v.detail, "proposal": v.proposal, "eligible_at": _iso(v.eligible_at)}
    if i is not None:
        d.update({"status": i.status, "hold_reason": i.hold_reason, "blocked_reason": i.blocked_reason, "cycle_state": i.cycle_state,
                  "error_code": i.cause_code, "error_at": _iso(i.cause_at), "asin_count": i.asin_count, "release_count": i.release_count,
                  "recent_auto_releases": i.recent_auto_releases, "last_run": i.last_run})
    return d


def _collect_aux(conn, ids: list[int], market: str) -> tuple[dict, dict, dict]:
    """(最後の失敗run, 取得済みASIN数, RELEASEイベント)を、対象の出典だけについて集める。読み取りのみ。"""
    cause, asins, events = {}, {}, {}
    if not ids:
        return cause, asins, events
    for r in conn.execute(text(
            "SELECT DISTINCT ON (source_id) source_id, id, error_code, completed_at FROM collector_run "
            "WHERE marketplace = :m AND source_id = ANY(:ids) AND status = 'FAILED' AND completed_at IS NOT NULL "
            "ORDER BY source_id, completed_at DESC, id DESC"), {"m": market, "ids": ids}):
        cause[r.source_id] = (r.error_code, r.completed_at)
    for r in conn.execute(text(
            "SELECT r.source_id, count(DISTINCT a.asin) AS n FROM collector_asin a JOIN collector_run r ON r.id = a.collector_run_id "
            "WHERE r.marketplace = :m AND r.source_id = ANY(:ids) GROUP BY r.source_id"), {"m": market, "ids": ids}):
        asins[r.source_id] = r.n
    for r in conn.execute(text(
            "SELECT source_id, event_type, actor, occurred_at FROM acquisition_source_event "
            "WHERE marketplace = :m AND source_id = ANY(:ids) AND event_type = 'RELEASE'"), {"m": market, "ids": ids}):
        events.setdefault(r.source_id, []).append((r.event_type, r.actor, r.occurred_at))
    return cause, asins, events


def build_market_plan(factory, market: str, cfg: WorkflowConfig, limit: int | None = None) -> MarketPlan:
    """1つの市場の保留・要解除の出典を全て判定する（読み取り専用）。limit=候補として表示する最大件数（超えた分は件数だけ残す）。"""
    from acquisition.eligibility import resolve_cycle

    plan = MarketPlan(market)
    with readonly_session(factory, market) as conn:
        now = db_now(conn)
        plan.now = now.isoformat()
        cfgs = load_configs(conn, market)
        scoped = []     # (facts, decision)
        for f in load_facts(conn, market):
            c = cfgs.get(f.source_type)
            if c is None:
                continue
            d = decide(f, c, now)
            codes = [b.code for b in d.blockers]
            if classify_hold(f.status, f.hold_reason, f.blocked_reason, codes)[0] is not None:
                scoped.append((f, d, c))
        cause, asins, events = _collect_aux(conn, [f.source_id for f, _d, _c in scoped], market)
        w, n = cfg.stall.burst_window_hours, cfg.stall.burst_min_sources
        bursts = detect_error_bursts(load_error_burst_events(conn, market, now, w), now, w, n)
    plan.burst = {"window_hours": w, "min_sources": n, "codes": {c: len(ids) for c, ids in bursts.items()}}
    ctx = RecoveryContext(market, now, frozenset(bursts), cfg.recovery.cooldown_hours, cfg.recovery.max_auto_releases,
                          cfg.price_band_for(market) is not None)
    for f, d, c in scoped:
        cyc = resolve_cycle(f.runs, now, c)
        last = max(f.runs, key=lambda r: r.run_id, default=None)
        code, at = cause.get(f.source_id, (None, None))
        ev = events.get(f.source_id, [])
        info = SourceInfo(
            f.source_id, market, f.source_type, f.display_primary + (f" / {f.display_qualifier}" if f.display_qualifier else ""),
            f.status, f.hold_reason, f.blocked_reason, tuple(b.code for b in d.blockers), cyc.state if cyc else None,
            d.running_run_id is not None, code, at, asins.get(f.source_id, 0), len(ev),
            count_recent_auto_releases(ev, now, cfg.recovery.window_days),
            {"run_id": last.run_id, "status": last.status, "outcome": last.outcome, "completed_at": _iso(last.completed_at),
             "cycle_state": last.cycle_state} if last else None)
        v = judge(info, ctx)
        if v is not None:
            plan.verdicts.append(v)
    plan.verdicts.sort(key=lambda v: (v.verdict != CANDIDATE, v.info.cause_at or now, v.source_id))   # 候補を先に、古い失敗から
    cands = [v for v in plan.verdicts if v.verdict == CANDIDATE]
    plan.candidates_total = len(cands)
    if limit is not None and len(cands) > limit:
        omitted = {v.source_id for v in cands[limit:]}
        plan.verdicts = [v for v in plan.verdicts if v.source_id not in omitted]
        plan.candidates_omitted_by_limit = len(omitted)
    return plan


def build_plans(factory, markets, cfg: WorkflowConfig, limit: int | None = None) -> dict[str, MarketPlan]:
    """市場ごとに独立して判定する。ある市場の失敗（例外）はその市場のerrorに入れ、他の市場は続ける。"""
    out: dict[str, MarketPlan] = {}
    for m in markets:
        try:
            out[m] = build_market_plan(factory, m, cfg, limit)
        except Exception as exc:  # noqa: BLE001 - 一方の市場の失敗で他方を止めない
            out[m] = MarketPlan(m, error=f"{type(exc).__name__}: {exc}"[:300])
    return out


# ---------------------------------------------------------------------- 表示

MAX_INTENTIONAL_LINES = 10


def format_plans(plans: dict[str, MarketPlan], limit: int | None = None) -> list[str]:
    lines = ["自動保留の復旧候補（ドライラン）: 実行はしません。DBの更新・Amazonへのアクセスはありません（読み取り専用）。"
             "解除する場合は、人が確認のうえ `python -m acquisition.cli release-hold --market <US|JP> --source-id <ID> --reason ...` を実行してください。"]
    if limit is not None:
        lines.append(f"（候補の表示は1市場あたり最大{limit}件）")
    for m, p in plans.items():
        lines.append("")
        if p.error:
            lines.append(f"[{m}] 判定に失敗: {p.error}")
            continue
        s = p.summary()
        lines.append(f"[{m}] 対象{s['in_scope']}件（自動保留{s['auto_hold']}・意図的な保留{s['intentional_hold']}） → 候補{s['candidates']}件・対象外{s['excluded']}件  （DB時刻 {p.now}）")
        bc = p.burst.get("codes") or {}
        lines.append(f"    環境障害の疑い（直近{p.burst.get('window_hours')}時間・{p.burst.get('min_sources')}出典以上）: "
                     + ("・".join(f"{c}({n}出典)" for c, n in bc.items()) if bc else "なし"))
        if s["excluded_by_reason"]:
            lines.append("    対象外の内訳: " + "・".join(f"{REASON_LABELS.get(r, r)} {n}件" for r, n in s["excluded_by_reason"].items()))
        cands = [v for v in p.verdicts if v.verdict == CANDIDATE]
        for v in cands:
            lines.append("    候補  " + _line(v))
        if p.candidates_omitted_by_limit:
            lines.append(f"    ほか候補{p.candidates_omitted_by_limit}件は --limit のため表示していません")
        shown_int = 0
        for v in (x for x in p.verdicts if x.verdict == EXCLUDED):
            if v.kind == KIND_INTENTIONAL:
                shown_int += 1
                if shown_int > MAX_INTENTIONAL_LINES:
                    continue
            lines.append("    対象外 " + _line(v))
        if shown_int > MAX_INTENTIONAL_LINES:
            lines.append(f"    （意図的な保留は上の{MAX_INTENTIONAL_LINES}件のみ表示。ほか{shown_int - MAX_INTENTIONAL_LINES}件は --out のJSONを参照）")
    return lines


def _line(v: Verdict) -> str:
    i = v.info
    lr = i.last_run or {}
    head = f"#{v.source_id} {v.source_type} {v.name} 保留={i.status}/{i.hold_reason or i.blocked_reason or '-'} "
    body = (f"直近run={lr.get('run_id', '-')}({lr.get('status', '-')}) 一巡={i.cycle_state or '-'} error={i.cause_code or '-'} "
            f"取得済みASIN={i.asin_count} 解除{i.release_count}回(自動{i.recent_auto_releases})")
    tail = f" → {v.proposal}" if v.proposal else f" → {REASON_LABELS.get(v.reason, v.reason)}" + (f"（{v.detail}）" if v.detail else "")
    if v.eligible_at:
        tail += f" 候補になる時刻 {v.eligible_at:%Y-%m-%d %H:%M} UTC以降"
    return head + body + tail


def dump_json(plans: dict[str, MarketPlan], path) -> None:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"dry_run": True, "markets": {m: p.to_dict() for m, p in plans.items()}}, ensure_ascii=False, indent=2),
                          encoding="utf-8")

