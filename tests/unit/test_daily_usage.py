"""日次使用量（JST区切り・確保/確定/解放・永続・同時確保）の単体テスト。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from workflow.daily_usage import DailyUsage, jst_day, next_jst_midnight

CAPS = {"pages": 5, "submit:EXPORT": 2, "asins": 100}


class Clock:
    def __init__(self, iso: str) -> None:
        self.t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def now(self):
        return self.t


def test_jst_day_boundary_is_midnight_in_tokyo():
    assert jst_day(datetime(2026, 10, 17, 14, 59, 59, tzinfo=timezone.utc)) == "2026-10-17"   # 23:59:59 JST
    assert jst_day(datetime(2026, 10, 17, 15, 0, 0, tzinfo=timezone.utc)) == "2026-10-18"     # 00:00:00 JST
    assert next_jst_midnight(datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)


def test_reserve_commit_release_and_caps_are_all_or_nothing(tmp_path):
    u = DailyUsage(tmp_path / "u.jsonl", now=Clock("2026-10-17T01:00:00Z").now)
    assert u.reserve("a", {"pages": 3}, CAPS)[0]
    ok, why = u.reserve("b", {"pages": 3, "asins": 10}, CAPS)
    assert not ok and "上限" in why and u.used() == {"pages": 3}  # 1種別でも超えれば何も確保しない
    u.commit("a", {"pages": 1})  # 実際に使った分に減らし、残りを返す
    assert u.used() == {"pages": 1}
    assert u.reserve("b", {"pages": 4, "asins": 10}, CAPS)[0] and u.used()["pages"] == 5
    u.release("b")
    assert u.used() == {"pages": 1} and u.has("b") == "RELEASED"
    assert u.reserve("a", {"pages": 99}, CAPS) == (True, "既に確保済み")  # 同じridは二重に確保しない（再開）
    assert u.reserve("c", {"pages": 99}, {"pages": None})[0]  # null=無制限


def test_unfinished_reservations_count_as_used_and_survive_restart(tmp_path):
    clock = Clock("2026-10-17T01:00:00Z")
    u = DailyUsage(tmp_path / "u.jsonl", now=clock.now)
    assert u.reserve("crashed", {"pages": 2, "submit:EXPORT": 1}, CAPS)[0]   # 確定されないまま（プロセス死・SAVE後の結果不明）
    u2 = DailyUsage(tmp_path / "u.jsonl", now=clock.now)                      # 再起動・新しいworkflow相当
    assert u2.used() == {"pages": 2, "submit:EXPORT": 1}
    assert u2.reserve("x", {"submit:EXPORT": 2}, CAPS)[0] is False            # 使用済みとして上限判定に効く（過少計上しない）


def test_usage_switches_at_the_jst_midnight_and_a_commit_stays_on_the_reservation_day(tmp_path):
    clock = Clock("2026-10-17T14:59:00Z")  # 23:59 JST
    u = DailyUsage(tmp_path / "u.jsonl", now=clock.now)
    assert u.reserve("late", {"pages": 5}, CAPS)[0]
    assert u.reserve("more", {"pages": 1}, CAPS)[0] is False                  # 同じ日(JST)の上限
    clock.t = datetime(2026, 10, 17, 15, 0, 1, tzinfo=timezone.utc)           # 日付が変わった（00:00:01 JST）
    assert u.used() == {} and u.reserve("next", {"pages": 5}, CAPS)[0]        # 新しい日の枠
    u.commit("late", {"pages": 2})                                            # 実行中に日付が変わっても、確保した日の分として確定
    assert u.used(jst_day(clock.t)) == {"pages": 5} and u.used("2026-10-17") == {"pages": 2}


def test_concurrent_reservations_never_exceed_the_cap(tmp_path):
    clock = Clock("2026-10-17T01:00:00Z")
    results = []

    def worker(i):
        u = DailyUsage(tmp_path / "u.jsonl", now=clock.now)  # 別プロセスを模した別インスタンス
        results.append(u.reserve(f"r{i}", {"pages": 1}, {"pages": 5})[0])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(results) == 5 and DailyUsage(tmp_path / "u.jsonl", now=clock.now).used() == {"pages": 5}


def test_used_since_sums_this_session_across_days(tmp_path):
    clock = Clock("2026-10-17T14:00:00Z")
    u = DailyUsage(tmp_path / "u.jsonl", now=clock.now)
    u.reserve("old", {"pages": 1}, CAPS)
    clock.t = datetime(2026, 10, 17, 14, 50, tzinfo=timezone.utc)
    u.reserve("s1", {"pages": 2}, CAPS)
    clock.t = datetime(2026, 10, 17, 15, 10, tzinfo=timezone.utc)
    u.reserve("s2", {"pages": 3}, CAPS)
    assert u.used_since("2026-10-17T14:30:00Z") == {"pages": 5}
