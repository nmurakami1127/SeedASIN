"""環境障害（多数の出典で同時に起きる一時的なアクセス障害）の判定。純粋関数と設定の読み込み（docs/DEVIATIONS.md #90）。

背景: Amazon側・ネットワーク・自分のIP/セッションの一時的な問題で、多数の出典が同じ時間帯に PAGE_LOAD_FAILED / SERVER_ERROR で失敗すると、
出典ごとの連続失敗（`acquisition.eligibility.decide`の`consecutive_failures`）が積み上がり、AUTO_FAILURES で一斉に自動保留されてしまう。
このモジュールは「その失敗は環境障害の疑いがあるか」を判定する。**判定は読み取りだけ**で、runの記録・cursor・取得済みASINは一切変えない。
連続失敗の数え方から除くだけ（冷却=COOLDOWNは従来どおり全ての失敗から数える。障害中に同じ出典を即座に取り直さないため）。

判定式（run r = 出典s・error_code=c・完了時刻t について）:
    c が ENV_FAILURE_CODES に含まれ、かつ
    同じ市場で、c と同じ error_code の失敗が [t - W時間, t + W時間]（両端を含む）に、異なる出典で N 個以上（rの出典sも1つとして数える）
    → r は環境障害の疑い（連続失敗にも自動保留の根拠にも数えない）。同じ出典の繰り返しは1出典と数える。

- 対象のerror_codeは一時的なもの（PAGE_LOAD_FAILED・SERVER_ERROR）だけ。REPEATED_PAGE・OVERLAP_MISMATCH・CURSOR系・CAPTCHA_UNSOLVED等、
  出典固有／設計上の失敗は、同時多発しても常に数える。
- 既に付いた自動保留（status=ON_HOLD）は、この判定では外れない（`decide`は status=ACTIVE の出典にだけ should_auto_hold を立てる）。
  判定は「新しい自動保留を付けない・連続失敗に加算しない」方向にだけ働く。
- 失敗の前後の窓に、後から他の出典の失敗が増えて環境障害と分かることがある（遡って数えなくなる）。失敗の記録は追記のみで窓は固定のため、
  一度数えなくなった失敗が（設定を変えない限り）後で再び数えられることはない。逆に、障害の初期（まだ他の出典の失敗が出ていない時点）に
  3回目の失敗に達した出典は、従来どおり自動保留される（既に付いた自動保留は外さない）。
- 設定は DB を使わず、JSONファイル（既定 config/env_failure_guard.json。環境変数 SEEDASIN_ENV_FAILURE_GUARD_CONFIG で場所を変更）。
  ファイルが無い／壊れている場合は**無効**（従来どおり）。既定は無効。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Connection, text

log = logging.getLogger(__name__)

# 一時的なアクセス障害で起きるコード（asin_collector/source_runner.py の _get が出すもの）。
# 出典固有／設計上の失敗（REPEATED_PAGE・OVERLAP_MISMATCH・CURSOR_*・EMPTY_PAGE_AFTER_RESULTS・NO_NEW_ASINS_LOOP・REVISITED_URL・
# BAND_*・CAPTCHA_UNSOLVED・EXCEPTION 等）は含めない。CAPTCHAは人の対応を伴う別の扱い（CLAUDE.md）。
ENV_FAILURE_CODES = frozenset({"PAGE_LOAD_FAILED", "SERVER_ERROR"})

GUARD_CONFIG_ENV = "SEEDASIN_ENV_FAILURE_GUARD_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "env_failure_guard.json"

# stall.py の burst_window_hours / burst_min_sources と同じ既定・範囲（食い違いを避ける）。
DEFAULT_WINDOW_HOURS = 3
DEFAULT_MIN_SOURCES = 5
WINDOW_HOURS_RANGE = (1, 168)
MIN_SOURCES_RANGE = (2, 1000)


class EnvGuardConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EnvGuardSettings:
    enabled: bool = False                       # 既定は無効（従来の挙動）
    window_hours: int = DEFAULT_WINDOW_HOURS
    min_sources: int = DEFAULT_MIN_SOURCES

    def validate(self) -> "EnvGuardSettings":
        if not isinstance(self.enabled, bool):
            raise EnvGuardConfigError(f"enabled は true/false で指定してください（指定: {self.enabled!r}）")
        for name, (lo, hi) in (("window_hours", WINDOW_HOURS_RANGE), ("min_sources", MIN_SOURCES_RANGE)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise EnvGuardConfigError(f"{name} は{lo}〜{hi}の整数で指定してください（指定: {v!r}）")
        return self


DISABLED = EnvGuardSettings()


def settings_from_dict(d: dict) -> EnvGuardSettings:
    unknown = set(d) - {"enabled", "window_hours", "min_sources"}
    if unknown:
        raise EnvGuardConfigError(f"未知の設定: {sorted(unknown)}")
    return EnvGuardSettings(d.get("enabled", False), d.get("window_hours", DEFAULT_WINDOW_HOURS),
                            d.get("min_sources", DEFAULT_MIN_SOURCES)).validate()


def load_env_guard_settings(path: str | Path | None = None, strict: bool = False) -> EnvGuardSettings:
    """設定を読む。ファイルが無ければ無効。壊れている場合は strict=False なら警告を出して**無効**（取得の終了処理を設定の誤りで失敗させない）、
    strict=True なら EnvGuardConfigError。"""
    p = Path(path) if path is not None else Path(os.environ.get(GUARD_CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    if not p.exists():
        return DISABLED
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise EnvGuardConfigError("設定はJSONオブジェクトで書いてください")
        return settings_from_dict(raw)
    except (OSError, ValueError) as exc:  # JSONDecodeError・EnvGuardConfigErrorはValueError
        if strict:
            raise EnvGuardConfigError(f"{p}: {exc}") from exc
        log.warning("env_failure_guard の設定を読めないため無効として扱います（%s: %s）", p, exc)
        return DISABLED


# ---------------------------------------------------------------------- 純粋関数

FailureEvent = tuple[int, "str | None", datetime]   # (source_id, error_code, 失敗の完了時刻)


def _sources_by_code(events: list[FailureEvent], start: datetime, end: datetime) -> dict[str, set[int]]:
    """[start, end]（両端を含む）に完了した失敗を、error_codeごとの出典の集合にする。error_code・時刻がNoneの行は無視。"""
    by_code: dict[str, set[int]] = {}
    for sid, code, at in events:
        if code is None or at is None or at < start or at > end:
            continue
        by_code.setdefault(code, set()).add(sid)
    return by_code


def detect_error_bursts(events: list[FailureEvent], as_of: datetime, window_hours: int, min_sources: int) -> dict[str, list[int]]:
    """バースト（環境障害の疑い）の判定。純粋関数（workflow/stall.py の警告・ドライランが使う。判定の元は`is_env_failure`と同じ`_sources_by_code`）。

    窓は `as_of - window_hours <= 完了時刻 <= as_of`（ちょうど窓の開始時刻は窓の内側。as_ofより未来は数えない）。
    同じerror_codeが `min_sources` 個以上の**異なる出典**で出ていれば、そのコードをバーストとして返す（ちょうどその数で成立）。
    同じ出典の失敗が何回出ても1出典として数える。戻り値は {error_code: [source_id, ...昇順]}（バーストのコードだけ）。error_codeがNoneの行は無視。
    """
    by_code = _sources_by_code(events, as_of - timedelta(hours=window_hours), as_of)
    return {c: sorted(ids) for c, ids in sorted(by_code.items()) if len(ids) >= min_sources}


def is_env_failure(source_id: int, error_code: str | None, completed_at: datetime | None, events: list[FailureEvent],
                   settings: EnvGuardSettings) -> bool:
    """1件の失敗が環境障害の疑いか（上の判定式）。無効設定・対象外のコード・時刻なしはFalse。自分の出典は常に1出典として数える。"""
    if not settings.enabled or completed_at is None or error_code not in ENV_FAILURE_CODES:
        return False
    w = timedelta(hours=settings.window_hours)
    sources = _sources_by_code(events, completed_at - w, completed_at + w).get(error_code, set()) | {source_id}
    return len(sources) >= settings.min_sources


def env_failure_run_ids(source_id: int, runs: list[tuple[int, str | None, datetime | None]], events: list[FailureEvent],
                        settings: EnvGuardSettings) -> frozenset[int]:
    """出典の失敗run (run_id, error_code, completed_at) のうち、環境障害の疑いのあるrun_idの集合。"""
    if not settings.enabled or not events:
        return frozenset()
    return frozenset(rid for rid, code, at in runs if is_env_failure(source_id, code, at, events, settings))


# ---------------------------------------------------------------------- DB（読み取りのみ）

def load_env_failure_events(conn: Connection, marketplace: str) -> list[FailureEvent]:
    """その市場の、環境障害のコードで終了した出典つきの失敗run（collector_run.status='FAILED'）。読み取りのみ。
    stall.load_error_burst_events と同じ条件（status='FAILED'・source_idあり）で、窓は判定側で絞る。
    呼び出し元の接続は`SessionFactory.get_session(marketplace)`（RLSで当該市場に限る）。"""
    rows = conn.execute(
        text("SELECT source_id, error_code, completed_at FROM collector_run WHERE marketplace = :m AND source_id IS NOT NULL "
             "AND status = 'FAILED' AND error_code = ANY(:codes) AND completed_at IS NOT NULL"),
        {"m": marketplace, "codes": sorted(ENV_FAILURE_CODES)},
    ).all()
    return [(r.source_id, r.error_code, r.completed_at) for r in rows]
