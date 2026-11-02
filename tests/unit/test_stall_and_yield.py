"""止まりの検知（workflow/stall.py・watch.py）・出典ごとの新規ASIN率の出力（seller_performance/source_yield.py）・稼働の可視化の単体テスト。

DB不要の部分: 警告のしきい値の境界、7日分が足りない場合、CSVの文字コード（UTF-8 BOM）、設定の既定値と範囲、通知の間引き、稼働の集計。
DBが要る部分（初見／既知の判定・US/JPの分離・要解除の検出）は tests/integration/test_source_yield_and_stall.py。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from seller_performance.source_yield import CSV_HEADER, YieldReport, YieldRow, format_report, write_csv
from seller_performance.watch_activity import NO_RECORD, build_activity, fetch_hours, format_activity
from workflow.cli import stall_status_lines, watch_status_lines
from workflow.config import ConfigError, config_from_dict
from workflow.stall import (MarketStall, count_leading_server_errors, has_seven_days, judge_new_asin, server_error_warning)
from workflow.watch import STALL_CHECK_MIN_MINUTES, CycleResult, Watcher, read_status
from workflow.watch_events import append_event, read_events

UTC = timezone.utc


# ---------------------------------------------------------------- しきい値の境界

def test_server_error_streak_counts_only_the_leading_run():
    assert count_leading_server_errors([]) == 0
    assert count_leading_server_errors(["SERVER_ERROR"] * 3 + [None, "SERVER_ERROR"]) == 3
    assert count_leading_server_errors([None, "SERVER_ERROR", "SERVER_ERROR"]) == 0           # 先頭が成功なら連続は途切れる
    assert count_leading_server_errors(["SERVER_ERROR", "PAGE_LOAD_FAILED", "SERVER_ERROR"]) == 1  # 別のエラーでも途切れる


@pytest.mark.parametrize("streak,expected", [(0, False), (4, False), (5, True), (6, True)])
def test_server_error_warning_boundary_default_5(streak, expected):
    assert server_error_warning(streak, 5) is expected


def test_server_error_warning_uses_the_configured_threshold():
    assert server_error_warning(2, 2) and not server_error_warning(2, 3)


WEEK = [100, 100, 100, 100, 100, 100, 100]  # 中央値100


@pytest.mark.parametrize("last24,status", [(0, "LOW"), (29, "LOW"), (30, "OK"), (31, "OK"), (100, "OK"), (500, "OK")])
def test_new_asin_boundary_is_strictly_below_30_percent_of_the_median(last24, status):
    j = judge_new_asin(last24, WEEK, 30)
    assert j.status == status and j.median == 100.0


def test_new_asin_boundary_follows_the_configured_percent():
    assert judge_new_asin(49, WEEK, 50).status == "LOW" and judge_new_asin(50, WEEK, 50).status == "OK"


def test_new_asin_uses_the_median_not_the_mean():
    # 平均は大きく引き上げられるが、中央値は100のまま（外れ値の日があっても基準が歪まない）
    j = judge_new_asin(29, [0, 100, 100, 100, 100, 100, 10_000], 30)
    assert j.median == 100.0 and j.status == "LOW"
    assert judge_new_asin(30, [0, 100, 100, 100, 100, 100, 10_000], 30).status == "OK"


def test_new_asin_zero_median_never_warns():
    j = judge_new_asin(0, [0, 0, 0, 0, 0, 0, 5], 30)  # 中央値0: 比べられないので警告しない
    assert j.status == "OK" and j.median == 0.0 and j.percent_of_median is None


@pytest.mark.parametrize("prior", [None, [], [100] * 6])
def test_new_asin_is_unknown_when_seven_days_are_not_available(prior):
    j = judge_new_asin(0, prior, 30)
    assert j.status == "UNKNOWN" and j.median is None and "足りません" in j.reason  # 0件でも警告ではなく判定不能


def test_seven_days_of_data_means_the_earliest_run_is_at_least_eight_days_old():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert not has_seven_days(None, now)
    assert not has_seven_days(now - timedelta(days=8) + timedelta(seconds=1), now)
    assert has_seven_days(now - timedelta(days=8), now)
    assert has_seven_days(now - timedelta(days=60), now)


# ---------------------------------------------------------------- 設定: 既定値と範囲

def _cfg(extra=None, tmp_path=None):
    d = {"lanes": {"US": {"origin_import": 1}}}
    d.update(extra or {})
    return config_from_dict(d)


def test_stall_defaults_are_5_and_30_and_absent_section_keeps_the_existing_config_valid():
    c = _cfg()
    assert (c.stall.server_error_streak, c.stall.new_asin_min_percent) == (5, 30)
    assert config_from_dict(c.to_dict()).stall == c.stall           # 往復で変わらない
    old = c.to_dict()
    del old["stall"]                                                  # 項目追加前に保存された設定のスナップショット（resumeで読む）
    assert config_from_dict(old).stall.server_error_streak == 5


def test_stall_values_are_configurable():
    c = _cfg({"stall": {"server_error_streak": 3, "new_asin_min_percent": 50}})
    assert (c.stall.server_error_streak, c.stall.new_asin_min_percent) == (3, 50)


@pytest.mark.parametrize("bad", [{"server_error_streak": 0}, {"server_error_streak": 1001}, {"server_error_streak": 2.5},
                                 {"server_error_streak": True}, {"server_error_streak": "5"},
                                 {"new_asin_min_percent": 0}, {"new_asin_min_percent": 101}, {"new_asin_min_percent": None}])
def test_stall_values_outside_the_range_are_rejected(bad):
    with pytest.raises(ConfigError):
        _cfg({"stall": bad})


@pytest.mark.parametrize("ok", [{"server_error_streak": 1}, {"server_error_streak": 1000}, {"new_asin_min_percent": 1}, {"new_asin_min_percent": 100}])
def test_stall_range_edges_are_accepted(ok):
    _cfg({"stall": ok})


def test_unknown_stall_key_is_rejected():
    with pytest.raises(ConfigError):
        _cfg({"stall": {"typo": 1}})


# ---------------------------------------------------------------- watch: 通知の間引き・状態ファイル

class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)

    def now(self):
        return self.t


def _stall(market, *texts):
    return MarketStall(market, needs_release_count=1, needs_release=[{"source_id": 7, "source_type": "SELLER", "name": "S", "codes": ["ON_HOLD"], "hold_reason": "x"}],
                       new_asin={"status": "UNKNOWN", "last24": 3, "median7d": None, "percent_of_median": None, "threshold_percent": 30, "reason": "7日分の日次データが足りません"},
                       warnings=[(k, t) for k, t in texts])


def _watcher(tmp_path, clock, lines, check, **cfgx):
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "in"), **cfgx})
    return Watcher(cfg, object(), SimpleNamespace(), now=clock.now, out=lines.append, stall_check=check)


def test_stall_warnings_use_the_notify_interval_and_do_not_touch_the_human_wait_list(tmp_path):
    clock, lines, calls = Clock(), [], []

    def check(factory, markets, limits, now):
        calls.append((list(markets), limits.server_error_streak))
        return {"US": _stall("US", ("server_error", "US: 商品ページの取得がSERVER_ERRORで5回連続しています"))}

    w = _watcher(tmp_path, clock, lines, check)
    res = CycleResult()
    w._check_stalls(res)
    assert [x for x in lines if "[停止の兆候]" in x] and "SERVER_ERROR" in lines[-1]
    assert res.stall and res.human == []                       # 人手待ちの一覧・状態には入れない
    assert calls[0][0] == ["JP", "US"]                         # 既定はcross有効 → 反対側の市場も調べる
    # 次の周期（検知の確認間隔の前）: 同じ警告は表示しない。確認もしない（DBを毎周期は読まない）
    clock.t += timedelta(minutes=STALL_CHECK_MIN_MINUTES - 1)
    w._check_stalls(CycleResult())
    assert len([x for x in lines if "[停止の兆候]" in x]) == 1 and len(calls) == 1
    # 確認間隔を過ぎても、notify_minutes（既定360分）以内なら表示しない
    clock.t += timedelta(minutes=10)
    r3 = CycleResult()
    w._check_stalls(r3)
    assert len(calls) == 2 and len([x for x in lines if "[停止の兆候]" in x]) == 1 and r3.stall   # 状態には残る
    # notify_minutesを過ぎたらもう一度表示する
    clock.t += timedelta(minutes=360)
    w._check_stalls(CycleResult())
    assert len([x for x in lines if "[停止の兆候]" in x]) == 2


def test_a_new_kind_of_warning_is_shown_immediately_but_the_same_one_is_not_repeated(tmp_path):
    clock, lines = Clock(), []
    state = {"w": [("server_error", "US: A")]}
    w = _watcher(tmp_path, clock, lines, lambda f, m, l, n: {"US": _stall("US", *state["w"])})
    w._check_stalls(CycleResult())
    clock.t += timedelta(minutes=STALL_CHECK_MIN_MINUTES)
    state["w"] = [("server_error", "US: A"), ("new_asin_low", "US: B")]
    w._check_stalls(CycleResult())
    shown = [x for x in lines if "[停止の兆候]" in x]
    assert len(shown) == 2 and shown[1].endswith("US: B")


def test_stall_check_failure_does_not_stop_the_watch_and_is_reported(tmp_path):
    clock, lines = Clock(), []

    def boom(*a):
        raise RuntimeError("db down")

    w = _watcher(tmp_path, clock, lines, boom)
    res = CycleResult()
    w._check_stalls(res)                                       # 例外が外に出ない
    assert any("db down" in x for x in lines) and res.human == []
    w._write_status("idle", "x", res)
    assert "db down" in read_status(tmp_path)["stall"]["error"]


def test_one_market_failing_is_reported_while_the_other_is_still_checked(tmp_path):
    from workflow.stall import check_all

    class F:
        def get_session(self, market):
            raise RuntimeError(f"{market} is down") if market == "US" else RuntimeError("JP also fails differently")

    out = check_all(F(), ["US", "JP"], SimpleNamespace(server_error_streak=5, new_asin_min_percent=30), datetime(2026, 11, 3, tzinfo=UTC))
    assert set(out) == {"US", "JP"} and "US is down" in out["US"].error and "differently" in out["JP"].error


def test_status_file_has_a_stall_section_and_old_status_files_still_render(tmp_path):
    clock, lines = Clock(), []
    w = _watcher(tmp_path, clock, lines, lambda f, m, l, n: {"US": _stall("US", ("server_error", "US: X"))})
    res = CycleResult()
    w._check_stalls(res)
    w._write_status("idle", "対象なし", res)
    d = read_status(tmp_path)
    assert d["stall"]["markets"]["US"]["needs_release_count"] == 1 and d["stall"]["markets"]["US"]["warnings"] == ["US: X"]
    assert d["stall"]["markets"]["US"]["new_asin"]["status"] == "UNKNOWN"
    text = "\n".join(watch_status_lines(d))
    assert "判定不能" in text and "要解除1件" in text and "! US: X" in text and "#7" in text
    d.pop("stall")                                              # 古いwatchの状態ファイル
    assert "停止の兆候" not in "\n".join(watch_status_lines(d)) and stall_status_lines(None) == []


def test_status_without_stall_check_is_null_when_factory_is_missing(tmp_path):
    cfg = config_from_dict({"lanes": {"US": {"origin_import": 1}}, "state_dir": str(tmp_path), "submission_dir": str(tmp_path / "in")})
    w = Watcher(cfg, None, SimpleNamespace())
    res = CycleResult()
    w._check_stalls(res)                                        # factory無し（単体テスト用）では何もしない
    assert res.stall == [] and w._stall_snapshot is None


def test_human_wait_and_stop_events_are_recorded_only_when_actually_shown(tmp_path):
    clock, lines = Clock(), []
    w = _watcher(tmp_path, clock, lines, lambda *a: {})
    w._notify("k", "US discover:US: CAPTCHA", CycleResult(), "US")
    w._notify("k", "US discover:US: CAPTCHA", CycleResult(), "US")   # 間引かれた表示は記録しない
    evs = read_events(tmp_path)
    assert [(e["kind"], e.get("market")) for e in evs] == [("human_wait", "US")]


# ---------------------------------------------------------------- CSV（UTF-8 BOM）

def _report():
    start = datetime(2026, 11, 2, tzinfo=UTC)
    rep = YieldReport("US", 24, start, start + timedelta(hours=24))
    rep.items = [YieldRow("ブランドＡ / 日本語", 5, "BRAND_NAME", 2, 10, 200, 120, 30), YieldRow("（出典なし・旧経路 BRAND_PAGE）", None, "BRAND_PAGE", 1, 0, 5, 5, 5)]
    rep.total = YieldRow("合計", None, "", 3, 10, 205, 123, 33)
    return rep


def test_csv_is_utf8_with_bom_and_has_header_items_and_total_last(tmp_path):
    path = write_csv(_report(), tmp_path / "exports" / "source_yield", now=datetime(2026, 11, 3, 1, 2, 3))
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                     # UTF-8 BOM（Excelで開ける）
    assert raw.count(b"\xef\xbb\xbf") == 1
    text = raw.decode("utf-8-sig")
    rows = [r.split(",") for r in text.splitlines()]
    assert rows[0] == CSV_HEADER and "ブランドＡ / 日本語" in text
    assert rows[-1][2] == "合計" and rows[-1][5] == "205" and rows[-1][6:9] == ["123", "33", "90"]
    assert path.name == "source_yield_US_24h_20261103_010203.csv" and path.parent.name == "source_yield"
    assert rows[1][9] == "3.0000" and rows[2][9] == ""         # 初見/ページ（ページ0は空欄）


def test_report_text_has_total_ratios_and_elapsed_time():
    rep = _report()
    rep.elapsed_seconds = 1.234
    out = "\n".join(format_report(rep))
    assert "保存行のうち重複なしの割合: 60.0%" in out and "初見の割合: 26.8%" in out and "1.23秒" in out and "合計 | 3 | 10 | 205 | 123 | 33 | 90" in out


def test_ratios_are_blank_for_empty_denominators():
    r = YieldRow("x", 1, "SELLER", 1, 0, 0, 0, 0)
    assert r.new_per_page is None and r.unique_pct is None and r.new_pct is None


# ---------------------------------------------------------------- 稼働の可視化

def _write(p, lines):
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")


def test_fetch_hours_ignores_released_and_zero_committed_reservations():
    ev = [
        {"op": "reserve", "rid": "a", "t": "2026-11-02T00:10:00Z", "task": "discover:US", "amounts": {"pages": 3}},
        {"op": "reserve", "rid": "b", "t": "2026-11-02T01:10:00Z", "task": "acquire:JP", "amounts": {"pages": 3}},
        {"op": "release", "rid": "b"},
        {"op": "reserve", "rid": "c", "t": "2026-11-02T02:10:00Z", "task": "acquire:JP", "amounts": {"pages": 2}},
        {"op": "commit", "rid": "c", "amounts": {"pages": 0}},
        {"op": "reserve", "rid": "d", "t": "2026-11-02T03:10:00Z", "task": "weird", "amounts": {"pages": 1}},
        {"op": "reserve", "rid": "e", "t": "2026-11-02T04:10:00Z", "task": "x:JP", "amounts": {"submit:EXPORT": 1}},
    ]
    assert [(t.hour, m) for t, m in fetch_hours(ev)] == [(0, "US"), (3, None)]


def test_activity_counts_distinct_hours_per_market_per_jst_day_and_marks_unrecorded_days(tmp_path):
    # JST 11-03 00:30 と 00:50 は同じ1時間 / 01:10 は別の1時間。UTC 11-02 15:30 = JST 11-03 00:30
    _write(tmp_path / "daily_usage.jsonl", [
        {"op": "reserve", "rid": "1", "t": "2026-11-02T15:30:00Z", "task": "discover:US", "amounts": {"pages": 1}, "day": "2026-11-03"},
        {"op": "reserve", "rid": "2", "t": "2026-11-02T15:50:00Z", "task": "acquire:US", "amounts": {"pages": 1}, "day": "2026-11-03"},
        {"op": "reserve", "rid": "3", "t": "2026-11-02T16:10:00Z", "task": "acquire:JP", "amounts": {"pages": 1}, "day": "2026-11-03"},
        {"op": "reserve", "rid": "4", "t": "2026-11-02T16:20:00Z", "task": "acquire:US", "amounts": {"pages": 1}, "day": "2026-11-03"},
    ])
    append_event(tmp_path, datetime(2026, 11, 3, 1, 0, tzinfo=UTC), "watch_start")
    append_event(tmp_path, datetime(2026, 11, 3, 2, 0, tzinfo=UTC), "human_wait", "JP", detail="d")
    append_event(tmp_path, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "human_wait", "JP", detail="d")
    append_event(tmp_path, datetime(2026, 11, 3, 3, 0, tzinfo=UTC), "stop_request")
    rows = build_activity(tmp_path, today=date(2026, 11, 3), days=3)
    assert [r.day for r in rows] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    today = rows[-1]
    assert today.hours == {"US": 2, "JP": 1, "ALL": 2}  # 全体は市場をまたいで同じ時間枠を重ねて数えない and today.human_waits == {"US": 0, "JP": 2} and today.stop_requests == 1
    assert rows[0].hours["US"] is None and rows[0].stop_requests is None and rows[0].human_waits["JP"] is None   # 記録が始まる前
    out = "\n".join(format_activity(rows))
    assert NO_RECORD in out and "2026-11-03 | 2時間 | 1時間 | 2時間 | 1回 | 0回 | 2回" in out


def test_activity_without_any_log_says_no_record_instead_of_zero(tmp_path):
    rows = build_activity(tmp_path, today=date(2026, 11, 3), days=2)
    assert all(v is None for r in rows for v in (*r.hours.values(), *r.human_waits.values(), r.stop_requests))
    assert "0時間" not in "\n".join(format_activity(rows))


def test_event_log_ignores_broken_lines_and_never_raises_on_write_failure(tmp_path):
    p = tmp_path / "watch_events.jsonl"
    p.write_text('{"t":"2026-11-03T00:00:00Z","kind":"watch_start"}\nnot json\n{"x":1}\n', encoding="utf-8")
    assert len(read_events(tmp_path)) == 1
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert append_event(blocker / "sub", datetime(2026, 11, 3, tzinfo=UTC), "watch_start") is False   # ディレクトリを作れない → 例外にしない
    assert read_events(tmp_path / "none") is None
