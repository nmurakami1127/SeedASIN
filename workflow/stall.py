"""止まりの検知（読み取りのみ。docs/DEVIATIONS.md #79）。watchの状態表示（watch_status.json）とターミナルの通知に出す。

背景: USが3日間ほぼ止まっていたのに、商品ページ取得のSERVER_ERRORの連続と、取り直しの上限（CYCLE_RESTART_LIMIT / ON_HOLD）による
「要解除」に気付けなかった。次の3つを**市場ごとに**調べて警告にする（DBの読み取りだけ。Amazon・統合ツールには触れない）。

1. 要解除の出典: 取得の判定（`acquisition.eligibility.decide`）が CYCLE_RESTART_LIMIT または ON_HOLD で止めている出典のうち、**自動保留**
   （AUTO_FAILURES / AUTO_CYCLE_RESTARTS / ACTIVEのままCYCLE_RESTART_LIMIT）の件数と一覧。人が意図した保留（hold_reason=MANUAL・除外、
   セラー側のCOMPETITOR_SAME_SERVICE / RETRY_LATER / PERMANENTLY_EXCLUDED）は警告に数えず、別カウント（intentional_hold_count）にする（docs/DEVIATIONS.md #84）。
2. 商品ページ取得（discover。`collector_run.run_type='ASIN_PAGE'`）が、最近の終了済みの調査で SERVER_ERROR のまま何回連続しているか。
   `server_error_streak` 回以上で警告（境界: ちょうどその回数で警告）。途中の中断（日次枠の不足など）は数えず、別の結果（成功・別のエラー）で連続は途切れる。
3. 直近24時間の初見ASIN数が、その前の7日間の日次（24時間ごと）の中央値の `new_asin_min_percent`% 未満なら警告（境界: ちょうどその割合は警告しない）。
   7日分の日次データが足りない市場（最も古い取得runが8日前より新しい）は警告を出さず「判定不能」と表示する。中央値が0のときも警告しない。

4. バースト（環境障害の疑い）: 直近`burst_window_hours`時間に、異なる`burst_min_sources`出典以上で同じerror_code（特にPAGE_LOAD_FAILED）の失敗が出たか。
   ネットワークやAmazon側の一時的な障害で出典がまとめて失敗している可能性を示す（警告のみ。連続失敗のカウントや自動保留の挙動は変えない）。

US/JPは別々のDB接続（`SessionFactory.get_session(market)`）で独立に調べ、一方の失敗や遅延が他方の検知を止めない（失敗はその市場のerrorに入れる）。
警告の繰り返し表示は、既存の人手待ちと同じ`watch.notify_minutes`で抑える（watch.py側）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import text

from acquisition.eligibility import decide, db_now, load_configs, load_facts
from seller_performance.source_yield import first_seen_daily_counts

RELEASE_CODES = ("CYCLE_RESTART_LIMIT", "ON_HOLD")
AUTO_HOLD_REASONS = ("AUTO_FAILURES", "AUTO_CYCLE_RESTARTS")        # acquisition/lease.py が自動保留で記録する hold_reason
KIND_AUTO, KIND_INTENTIONAL = "AUTO", "INTENTIONAL"
PRIOR_DAYS = 7          # 比較に使う日次の窓の数（直近24時間の前の7日間）
STREAK_LOOKBACK = 1000  # 連続を数えるために読む終了済み調査の最大件数
MAX_LISTED = 50         # 状態ファイルに載せる要解除の一覧の最大件数（件数自体は全件）
STATUS_OK, STATUS_LOW, STATUS_UNKNOWN = "OK", "LOW", "UNKNOWN"


# ---------------------------------------------------------------------- 純粋関数（境界のテスト対象）

def count_leading_server_errors(error_codes: list[str | None]) -> int:
    """新しい順の終了済み調査のerror_codeから、先頭から連続するSERVER_ERRORの数を数える。"""
    n = 0
    for c in error_codes:
        if c != "SERVER_ERROR":
            break
        n += 1
    return n


def server_error_warning(streak: int, threshold: int) -> bool:
    return streak >= threshold


@dataclass(frozen=True)
class NewAsinJudgement:
    status: str                 # OK / LOW / UNKNOWN
    last24: int | None
    median: float | None
    percent_of_median: float | None
    reason: str = ""


def judge_new_asin(last24: int, prior_daily: list[int] | None, min_percent: int) -> NewAsinJudgement:
    """直近24時間の初見ASIN数を、その前の日次7日分の中央値と比べる。

    prior_dailyがNone、または7日分に満たなければ判定不能（UNKNOWN）。中央値が0なら比べられないのでOK（警告しない）。
    LOWは `last24 < 中央値 × min_percent%`（ちょうどその割合はOK。整数のまま比べて丸めの誤差を避ける）。
    """
    if prior_daily is None or len(prior_daily) < PRIOR_DAYS:
        return NewAsinJudgement(STATUS_UNKNOWN, last24, None, None, "7日分の日次データが足りません")
    med = float(median(prior_daily[:PRIOR_DAYS]))
    pct = None if med <= 0 else 100.0 * last24 / med
    if med > 0 and last24 * 100 < med * min_percent:
        return NewAsinJudgement(STATUS_LOW, last24, med, pct)
    return NewAsinJudgement(STATUS_OK, last24, med, pct)


def has_seven_days(earliest_run_at: datetime | None, as_of: datetime) -> bool:
    """比較に使う7日分（直近24時間の前の7日間 = as_ofの8日前から）の日次データがあるか。最も古い取得runがそれ以前なら足りている。"""
    return earliest_run_at is not None and earliest_run_at <= as_of - timedelta(days=PRIOR_DAYS + 1)


def classify_hold(status: str, hold_reason: str | None, blocked_reason: str | None, codes: list[str] | tuple[str, ...]) -> tuple[str | None, str]:
    """保留・除外・上限の出典を「自動」か「意図的」かに分ける。純粋関数。戻り値は (種類, 理由ラベル)。種類は AUTO / INTENTIONAL / None（どちらでもない）。

    意図的（人の判断。自動の復旧・警告の対象にしない）: hold_reason=MANUAL、EXCLUDED、セラー側の COMPETITOR_SAME_SERVICE / RETRY_LATER / PERMANENTLY_EXCLUDED、
    知らないhold_reason（安全側で意図的として扱う）。自動: AUTO_FAILURES / AUTO_CYCLE_RESTARTS、およびACTIVEのままCYCLE_RESTART_LIMITで止まっているもの。
    意図的の判定を先にする（自動保留の出典を人があとから競合等で止めた場合も意図的として扱う）。
    """
    if blocked_reason == "PERMANENTLY_EXCLUDED":
        return KIND_INTENTIONAL, "PERMANENTLY_EXCLUDED"
    if status == "EXCLUDED":
        return KIND_INTENTIONAL, "MANUAL_EXCLUDED"
    if blocked_reason in ("COMPETITOR_SAME_SERVICE", "RETRY_LATER"):
        return KIND_INTENTIONAL, f"SELLER_{blocked_reason}"
    if status == "ON_HOLD":
        if hold_reason == "MANUAL":
            return KIND_INTENTIONAL, "MANUAL_HOLD"
        if hold_reason in AUTO_HOLD_REASONS:
            return KIND_AUTO, hold_reason
        return KIND_INTENTIONAL, "UNKNOWN_HOLD_REASON"
    if status == "ACTIVE" and "CYCLE_RESTART_LIMIT" in codes:
        return KIND_AUTO, "ACTIVE_CYCLE_RESTART_LIMIT"
    return None, ""


def detect_error_bursts(events: list[tuple[int, str | None, datetime]], as_of: datetime, window_hours: int, min_sources: int) -> dict[str, list[int]]:
    """バースト（環境障害の疑い）の判定。純粋関数。events=(source_id, error_code, 失敗の完了時刻)。

    窓は `as_of - window_hours <= 完了時刻 <= as_of`（ちょうど窓の開始時刻は窓の内側。as_ofより未来は数えない）。
    同じerror_codeが `min_sources` 個以上の**異なる出典**で出ていれば、そのコードをバーストとして返す（ちょうどその数で成立）。
    同じ出典の失敗が何回出ても1出典として数える。戻り値は {error_code: [source_id, ...昇順]}（バーストのコードだけ）。error_codeがNoneの行は無視。
    """
    start = as_of - timedelta(hours=window_hours)
    by_code: dict[str, set[int]] = {}
    for sid, code, at in events:
        if code is None or at is None or at < start or at > as_of:
            continue
        by_code.setdefault(code, set()).add(sid)
    return {c: sorted(ids) for c, ids in sorted(by_code.items()) if len(ids) >= min_sources}


def load_error_burst_events(conn, market: str, as_of: datetime, window_hours: int) -> list[tuple[int, str, datetime]]:
    """窓の中で終了した、出典つきの失敗run（collector_run.status='FAILED'、error_codeあり）。読み取りのみ。"""
    rows = conn.execute(
        text("SELECT source_id, error_code, completed_at FROM collector_run WHERE marketplace = :m AND source_id IS NOT NULL "
             "AND status = 'FAILED' AND error_code IS NOT NULL AND completed_at >= :s AND completed_at <= :e"),
        {"m": market, "s": as_of - timedelta(hours=window_hours), "e": as_of}).all()
    return [(r.source_id, r.error_code, r.completed_at) for r in rows]


# ---------------------------------------------------------------------- 市場ごとの検知

@dataclass
class MarketStall:
    market: str
    needs_release_count: int = 0
    needs_release: list[dict] = field(default_factory=list)
    intentional_hold_count: int = 0      # 要解除の件数に含めない、意図的な保留（MANUAL・除外・セラー側の記録）の件数
    burst: dict = field(default_factory=dict)   # {"window_hours", "min_sources", "codes": {error_code: 出典数}}。空=バーストなし
    server_error_streak: int = 0
    server_error_last_at: str | None = None
    new_asin: dict = field(default_factory=dict)
    warnings: list[tuple[str, str]] = field(default_factory=list)   # (種類のキー, 表示文)。キーは「同じ警告」の判定（通知の間引き）に使う
    error: str | None = None

    def to_dict(self) -> dict:
        return {"needs_release_count": self.needs_release_count, "needs_release": self.needs_release,
                "intentional_hold_count": self.intentional_hold_count, "burst": self.burst,
                "server_error_streak": self.server_error_streak, "server_error_last_at": self.server_error_last_at,
                "new_asin": self.new_asin, "warnings": [t for _k, t in self.warnings], "error": self.error}


def _find_needs_release(conn, market: str) -> tuple[list[dict], int]:
    """要解除の自動保留の一覧と、除外した意図的な保留の件数。取得の判定が CYCLE_RESTART_LIMIT / ON_HOLD で止めている出典を、classify_holdで分ける。"""
    cfgs = load_configs(conn, market)
    now = db_now(conn)
    out = []
    intentional = 0
    for f in load_facts(conn, market):
        cfg = cfgs.get(f.source_type)
        if cfg is None:
            continue
        codes = [b.code for b in decide(f, cfg, now).blockers if b.code in RELEASE_CODES]
        if not codes:
            continue
        kind, _label = classify_hold(f.status, f.hold_reason, f.blocked_reason, codes)
        if kind == KIND_INTENTIONAL:
            intentional += 1
            continue
        out.append({"source_id": f.source_id, "source_type": f.source_type,
                    "name": f.display_primary + (f" / {f.display_qualifier}" if f.display_qualifier else ""),
                    "codes": sorted(set(codes)), "hold_reason": f.hold_reason or f.blocked_reason})
    return out, intentional


def _server_error_streak(conn, market: str) -> tuple[int, str | None]:
    rows = conn.execute(
        text(
            "SELECT error_code, completed_at FROM collector_run WHERE marketplace = :m AND run_type = 'ASIN_PAGE' "
            "AND status IN ('COMPLETED', 'FAILED') AND completed_at IS NOT NULL ORDER BY completed_at DESC, id DESC LIMIT :n"),
        {"m": market, "n": STREAK_LOOKBACK}).all()
    streak = count_leading_server_errors([r.error_code for r in rows])
    last = rows[0].completed_at if streak and rows else None
    return streak, last.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None


def check_market(factory, market: str, limits, as_of: datetime) -> MarketStall:
    """1つの市場を調べる。limitsは`workflow.config.Stall`（server_error_streak, new_asin_min_percent）。DBは読み取りだけ。"""
    res = MarketStall(market)
    with factory.get_session(market) as conn, conn.begin():
        items, res.intentional_hold_count = _find_needs_release(conn, market)
        res.needs_release_count, res.needs_release = len(items), items[:MAX_LISTED]
        window_h, min_src = getattr(limits, "burst_window_hours", 3), getattr(limits, "burst_min_sources", 5)
        now_db = db_now(conn)
        bursts = detect_error_bursts(load_error_burst_events(conn, market, now_db, window_h), now_db, window_h, min_src)
        res.burst = {"window_hours": window_h, "min_sources": min_src, "codes": {c: len(ids) for c, ids in bursts.items()}}
        res.server_error_streak, res.server_error_last_at = _server_error_streak(conn, market)
        counts, earliest = first_seen_daily_counts(conn, market, as_of, days=PRIOR_DAYS + 1)
    prior = [counts.get(k, 0) for k in range(1, PRIOR_DAYS + 1)] if has_seven_days(earliest, as_of) else None
    j = judge_new_asin(counts.get(0, 0), prior, limits.new_asin_min_percent)
    res.new_asin = {"status": j.status, "last24": j.last24, "median7d": j.median, "percent_of_median": j.percent_of_median,
                    "threshold_percent": limits.new_asin_min_percent, "reason": j.reason}
    if res.needs_release_count:
        head = "、".join(f"#{i['source_id']}({'/'.join(i['codes'])})" for i in items[:5])
        skipped = f"（うち意図的な保留{res.intentional_hold_count}件は除く）" if res.intentional_hold_count else ""
        res.warnings.append(("needs_release:" + ",".join(str(i["source_id"]) for i in res.needs_release),
                             f"{market}: 要解除の出典が{res.needs_release_count}件あります{skipped}（自動保留 CYCLE_RESTART_LIMIT/ON_HOLD）: {head}"
                            f"{' ほか' if res.needs_release_count > 5 else ''}。原因を確認して "
                            f"`python -m acquisition.cli release-hold --market {market} --source-id <ID> --reason ...`"))
    for code, n in res.burst["codes"].items():
        res.warnings.append((f"burst:{code}", f"{market}: 環境障害の疑い: 直近{window_h}時間に{n}出典で{code}の失敗が出ています"
                            f"（目安 {min_src}出典以上）。ネットワーク・Amazon側の状態を確認してください（個々の出典を解除する前に）"))
    if server_error_warning(res.server_error_streak, limits.server_error_streak):
        res.warnings.append(("server_error", f"{market}: 商品ページの取得がSERVER_ERRORで{res.server_error_streak}回連続しています"
                            f"（警告の目安 {limits.server_error_streak}回・最後 {res.server_error_last_at or '-'}）"))
    if j.status == STATUS_LOW:
        res.warnings.append(("new_asin_low", f"{market}: 直近24時間の初見ASINは{j.last24}件で、直近7日の日次の中央値{j.median:g}件の"
                            f"{j.percent_of_median:.0f}%です（{limits.new_asin_min_percent}%未満で警告）"))
    return res


def check_all(factory, markets, limits, as_of: datetime) -> dict[str, MarketStall]:
    """市場ごとに独立して調べる。ある市場の失敗（例外）はその市場のerrorに入れ、他の市場の検知は続ける。"""
    out: dict[str, MarketStall] = {}
    for m in markets:
        try:
            out[m] = check_market(factory, m, limits, as_of)
        except Exception as exc:  # noqa: BLE001 - 検知の失敗でwatchを止めない
            out[m] = MarketStall(m, error=f"{type(exc).__name__}: {exc}"[:200])
    return out
