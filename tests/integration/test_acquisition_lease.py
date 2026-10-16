"""取得実行の貸出(lease)の結合テスト（実PostgreSQL、使い捨てtest-db）。

claim・heartbeat・進捗保存・終了・中断・期限切れ回収、CAPTCHA待ち、回収との競合、旧プロセスの再開、
自動保留、旧経路の行との共存を検証する。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from acquisition.lease import LeaseLost, NotEligible
from acquisition.registry import SourceRegistry
from tests.integration.acq_helpers import (
    asin_count, expire_run, make_runs, new_source, run_row, set_config, unique_key,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


def test_claim_creates_a_leased_running_run_and_rejects_a_second_claim(app_role_factory, registry, runs):
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    row = run_row(app_role_factory, "US", c.run_id)
    assert (row.status, row.outcome, row.lease_ok, row.lease_seconds) == ("RUNNING", None, True, 300)
    with pytest.raises(NotEligible) as e:
        runs.claim("US", s.source_id)
    assert e.value.decision.state == "RUNNING"


def test_concurrent_claims_have_exactly_one_winner(registry, runs):
    s = new_source(registry)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def go():
        barrier.wait()
        try:
            runs.claim("US", s.source_id)
            results.append("ok")
        except NotEligible:
            results.append("refused")

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["ok", "refused"]


def test_claim_re_evaluates_at_start_so_a_stale_plan_cannot_double_fetch(registry, runs):
    s = new_source(registry)
    first = runs.claim("US", s.source_id)  # 別の実行が先に開始（planで見ていた状態は古くなる）
    runs.append_asins(first, ["B0TESTASN1"])
    runs.finish(first, "SUCCESS_NONEMPTY")
    with pytest.raises(NotEligible) as e:  # 取得間隔内
        runs.claim("US", s.source_id)
    assert e.value.decision.state == "TOO_RECENT"


def test_heartbeat_extends_but_never_resurrects_an_expired_lease(app_role_factory, registry, runs):
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    expire_run(app_role_factory, "US", c.run_id, seconds=3600)
    with pytest.raises(LeaseLost, match="失効"):
        runs.heartbeat(c)
    row = run_row(app_role_factory, "US", c.run_id)
    assert row.lease_ok is False and row.status == "RUNNING"  # 期限切れheartbeatで復活していない
    fresh = runs.claim("US", registry.register_source("US", "SELLER", unique_key()).source_id)
    runs.heartbeat(fresh)  # 有効な間は延長できる
    assert run_row(app_role_factory, "US", fresh.run_id).lease_ok


def test_expired_but_unreaped_run_rejects_append_and_finish(app_role_factory, registry, runs):
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    runs.append_asins(c, ["B0TESTASN1"])
    expire_run(app_role_factory, "US", c.run_id)
    with pytest.raises(LeaseLost):
        runs.append_asins(c, ["B0TESTASN2"])
    with pytest.raises(LeaseLost):
        runs.finish(c, "SUCCESS_NONEMPTY")
    assert asin_count(app_role_factory, "US", c.run_id) == 1  # 失効後の追記は入らない。失効前の分は保持
    assert run_row(app_role_factory, "US", c.run_id).status == "RUNNING"  # まだ回収前


def test_reaped_run_cannot_be_overwritten_by_the_resumed_old_process(app_role_factory, registry, runs):
    s = new_source(registry)
    old = runs.claim("US", s.source_id)
    runs.append_asins(old, ["B0OLDASIN01"])
    expire_run(app_role_factory, "US", old.run_id)
    assert runs.reap_expired("US", s.source_id) == [old.run_id]
    row = run_row(app_role_factory, "US", old.run_id)
    assert (row.status, row.outcome, row.interrupt_kind) == ("INTERRUPTED", "INTERRUPTED", "LEASE_EXPIRED")

    new = runs.claim("US", s.source_id)  # 回収後は再び実行できる（中断は間隔に数えない）
    runs.append_asins(new, ["B0NEWASIN01"])
    runs.finish(new, "SUCCESS_NONEMPTY")

    # 旧プロセスが再開しても、結果・成功状態・ASINを何も書けない
    for call in (lambda: runs.heartbeat(old), lambda: runs.append_asins(old, ["B0OLDASIN02"]),
                 lambda: runs.finish(old, "SUCCESS_NONEMPTY"), lambda: runs.enter_captcha_wait(old)):
        with pytest.raises(LeaseLost):
            call()
    assert runs.interrupt(old) is False  # 既に失われた実行の中断は何もしない
    assert (run_row(app_role_factory, "US", old.run_id).status, asin_count(app_role_factory, "US", old.run_id)) == ("INTERRUPTED", 1)
    assert run_row(app_role_factory, "US", new.run_id).outcome == "SUCCESS_NONEMPTY"


def test_db_trigger_blocks_rewrite_of_terminal_new_style_runs_even_via_raw_sql(app_role_factory, registry, runs):
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    runs.finish(c, "SUCCESS_NONEMPTY")
    with app_role_factory.get_session("US") as conn:
        for sql in ("UPDATE collector_run SET status = 'RUNNING', outcome = NULL WHERE id = :id",
                    "UPDATE collector_run SET outcome = 'FAILED', status = 'FAILED' WHERE id = :id",
                    "UPDATE collector_run SET asin_count = 99 WHERE id = :id"):
            with pytest.raises(DBAPIError, match="already terminal"):
                with conn.begin():
                    conn.execute(text(sql), {"id": c.run_id})


def test_reap_waits_for_an_in_flight_writer_and_then_reevaluates_expiry(app_role_factory, registry, runs):
    """書き込み中(行ロック保持)に回収が来ても、書き込みの確定を待つ。書き込みがleaseを延ばしていれば回収しない。"""
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    expire_run(app_role_factory, "US", c.run_id)
    done: list = []
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("SELECT 1 FROM collector_run WHERE id = :id FOR UPDATE"), {"id": c.run_id})
        t = threading.Thread(target=lambda: done.append(runs.reap_expired("US", s.source_id)))
        t.start()
        time.sleep(0.7)
        assert not done  # 行ロックのため回収は待っている（直列化）
        conn.execute(text("UPDATE collector_run SET lease_expires_at = clock_timestamp() + interval '5 minutes' WHERE id = :id"),
                     {"id": c.run_id})
    t.join(10)
    assert done == [[]]  # 延長が先に確定したので、回収されない
    assert run_row(app_role_factory, "US", c.run_id).status == "RUNNING"


def test_reap_after_inflight_append_keeps_the_saved_asins(app_role_factory, registry, runs):
    """書き込みがleaseの有効を確認した後、保存中に実時間でleaseが切れた場合: 回収は行ロックを待ち、
    書き込みの確定後に回収する。保存済みのASINは保持される。"""
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    with app_role_factory.get_session("US") as conn, conn.begin():  # 残り約1.2秒に調整（コミット済み）
        conn.execute(text("UPDATE collector_run SET lease_expires_at = clock_timestamp() + interval '1200 milliseconds' WHERE id = :id"),
                     {"id": c.run_id})
    done: list = []
    with app_role_factory.get_session("US") as conn, conn.begin():
        runs._lock_valid_run(conn, c)  # 追記トランザクションが有効なleaseを確認して行ロックを保持
        time.sleep(1.6)  # 保存中にleaseが実時間で切れる
        t = threading.Thread(target=lambda: done.append(runs.reap_expired("US", s.source_id)))
        t.start()
        time.sleep(0.7)
        assert not done  # 回収は行ロックを待っている
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :id, 'B0SAVEDASN')"),
                     {"id": c.run_id})
    t.join(10)
    assert done == [[c.run_id]]  # 確定済みの書き込みの後で回収
    assert asin_count(app_role_factory, "US", c.run_id) == 1
    assert run_row(app_role_factory, "US", c.run_id).status == "INTERRUPTED"


def test_captcha_wait_lease_heartbeat_expiry_and_resume(app_role_factory, registry, runs):
    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    runs.enter_captcha_wait(c)
    assert run_row(app_role_factory, "US", c.run_id).status == "CAPTCHA_WAIT"
    with app_role_factory.get_session("US") as conn, conn.begin():
        secs = conn.execute(text("SELECT extract(epoch FROM lease_expires_at - heartbeat_at) FROM collector_run WHERE id = :id"),
                            {"id": c.run_id}).scalar_one()
    assert 1790 <= secs <= 1800  # CAPTCHA待ちは長いlease（仮設定30分）
    runs.heartbeat(c)  # 待機中もheartbeatで延長（待機用のleaseを維持）
    runs.append_asins(c, ["B0TESTASN1"])  # 待機中の進捗保存も、有効なleaseの間は可
    runs.leave_captcha_wait(c)
    assert run_row(app_role_factory, "US", c.run_id).status == "RUNNING"
    with pytest.raises(LeaseLost):  # RUNNINGからleaveはできない
        runs.leave_captcha_wait(c)

    c2 = runs.claim("US", registry.register_source("US", "SELLER", unique_key()).source_id)
    runs.enter_captcha_wait(c2)
    expire_run(app_role_factory, "US", c2.run_id)  # 待機中にプロセスが死んだ/長く放置された
    assert runs.reap_expired("US", c2.source_id) == [c2.run_id]
    with pytest.raises(LeaseLost):  # 回収後に人がCAPTCHAを解いて再開しても、RUNNINGへ戻れない
        runs.leave_captcha_wait(c2)
    assert run_row(app_role_factory, "US", c2.run_id).status == "INTERRUPTED"


def test_abort_run_is_for_new_style_runs_only_and_legacy_running_rows_are_untouched(app_role_factory, registry, runs):
    with app_role_factory.get_session("US") as conn, conn.begin():
        legacy_id = conn.execute(
            text("INSERT INTO collector_run (marketplace, run_type, target, status) VALUES ('US','BRAND_PAGE','https://www.amazon.com/s?k=x','RUNNING') RETURNING id")
        ).scalar_one()
    assert runs.reap_expired("US") == [] or legacy_id not in runs.reap_expired("US")
    with pytest.raises(ValueError, match="旧経路"):
        runs.abort_run("US", legacy_id, "x")
    assert run_row(app_role_factory, "US", legacy_id).status == "RUNNING"  # 旧RUNNINGは変更しない

    s = new_source(registry)
    c = runs.claim("US", s.source_id)
    with pytest.raises(ValueError, match="理由"):
        runs.abort_run("US", c.run_id, " ")
    runs.abort_run("US", c.run_id, "プロセス終了を確認")
    assert run_row(app_role_factory, "US", c.run_id).interrupt_kind == "USER_ABORT"
    with pytest.raises(LeaseLost):
        runs.heartbeat(c)


def test_legacy_rows_coexist_with_the_guard(app_role_factory):
    """旧経路の行(source_id NULL)は従来どおり更新できる。source_idの付け替えは禁止。"""
    with app_role_factory.get_session("US") as conn, conn.begin():
        rid = conn.execute(
            text("INSERT INTO collector_run (marketplace, run_type, target, status) VALUES ('US','BRAND_PAGE','https://www.amazon.com/s?k=legacy','RUNNING') RETURNING id")
        ).scalar_one()
        conn.execute(text("UPDATE collector_run SET status = 'CAPTCHA_WAIT' WHERE id = :id"), {"id": rid})
        conn.execute(text("UPDATE collector_run SET status = 'COMPLETED', completed_at = now(), asin_count = 0 WHERE id = :id"), {"id": rid})
        conn.execute(text("UPDATE collector_run SET status = 'FAILED' WHERE id = :id"), {"id": rid})  # 旧経路は終了後でも従来どおり
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="immutable"):
            with conn.begin():
                conn.execute(text("UPDATE collector_run SET source_id = 1 WHERE id = :id"), {"id": rid})


def test_outcomes_map_to_status_and_completeness(app_role_factory, registry, runs):
    expect = {"SUCCESS_NONEMPTY": ("COMPLETED", "COMPLETE"), "SUCCESS_EMPTY": ("COMPLETED", "COMPLETE"),
              "PARTIAL": ("FAILED", "PARTIAL"), "FAILED": ("FAILED", None), "UNVERIFIED_EMPTY": ("FAILED", None)}
    set_config(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0, max_consecutive_failures=99)
    try:
        for outcome, (status, comp) in expect.items():
            c = runs.claim("US", new_source(registry).source_id)
            runs.finish(c, outcome, error_code="E1" if status == "FAILED" else None, pages_fetched=3)
            row = run_row(app_role_factory, "US", c.run_id)
            assert (row.status, row.outcome, row.completeness, row.pages_fetched) == (status, outcome, comp, 3)
        c = runs.claim("US", new_source(registry).source_id)
        with pytest.raises(ValueError, match="truncation_reason"):
            runs.finish(c, "SUCCESS_TRUNCATED")
        runs.finish(c, "SUCCESS_TRUNCATED", truncation_reason="PAGE_LIMIT", pages_fetched=30)
        row = run_row(app_role_factory, "US", c.run_id)
        assert (row.status, row.completeness) == ("COMPLETED", "TRUNCATED")
        with pytest.raises(ValueError):
            runs.finish(runs.claim("US", new_source(registry).source_id), "BOGUS")
    finally:
        set_config(app_role_factory, "US", "SELLER", retry_cooldown_minutes=60, max_consecutive_failures=3)


def test_results_and_the_thirty_day_limit_through_the_real_claim(registry, runs, app_role_factory):
    counted = {"SUCCESS_NONEMPTY": "TOO_RECENT", "SUCCESS_EMPTY": "TOO_RECENT", "SUCCESS_TRUNCATED": "TOO_RECENT"}
    for outcome, state in counted.items():
        s = new_source(registry)
        c = runs.claim("US", s.source_id)
        runs.finish(c, outcome, truncation_reason="PAGE_LIMIT" if outcome == "SUCCESS_TRUNCATED" else None)
        with pytest.raises(NotEligible) as e:
            runs.claim("US", s.source_id)
        assert e.value.decision.state == state
    for outcome in ("PARTIAL", "FAILED", "UNVERIFIED_EMPTY"):
        s = new_source(registry)
        runs.finish(runs.claim("US", s.source_id), outcome)
        with pytest.raises(NotEligible) as e:  # 30日制限には数えない → 冷却(60分)が理由
            runs.claim("US", s.source_id)
        assert e.value.decision.state == "COOLDOWN"


def test_user_interrupt_and_lease_expiry_do_not_trigger_auto_hold_but_failures_do(app_role_factory, registry, runs):
    set_config(app_role_factory, "US", "SELLER", retry_cooldown_minutes=0)
    try:
        s = new_source(registry)
        for _ in range(4):  # ユーザー中断(Ctrl+C相当)は何回でも数えない
            c = runs.claim("US", s.source_id)
            assert runs.interrupt(c, "USER") is True
        c = runs.claim("US", s.source_id)
        expire_run(app_role_factory, "US", c.run_id)
        runs.reap_expired("US", s.source_id)  # lease失効も数えない
        c = runs.claim("US", s.source_id)
        runs.abort_run("US", c.run_id, "手動中断")  # abort_runも数えない
        assert runs.finish(runs.claim("US", s.source_id), "FAILED", error_code="E1") is None  # 1回目
        assert runs.finish(runs.claim("US", s.source_id), "PARTIAL", error_code="E2") is None  # 2回目
        held = runs.finish(runs.claim("US", s.source_id), "UNVERIFIED_EMPTY", error_code="E3")  # 3回目 → 自動保留
        assert held is not None and held.consecutive_failures == 3
        with app_role_factory.get_session("US") as conn, conn.begin():
            row = conn.execute(text("SELECT status, hold_reason, hold_detail FROM acquisition_source WHERE source_id = :id"),
                               {"id": s.source_id}).one()
            ev = conn.execute(text("SELECT event_type, actor FROM acquisition_source_event WHERE source_id = :id ORDER BY event_id"),
                              {"id": s.source_id}).all()
        assert (row.status, row.hold_reason) == ("ON_HOLD", "AUTO_FAILURES")
        assert "3回" in row.hold_detail and "E3" in row.hold_detail  # 根拠を表示
        assert [e.event_type for e in ev] == ["REGISTERED", "AUTO_HOLD"]
        with pytest.raises(NotEligible) as e:
            runs.claim("US", s.source_id)
        assert e.value.decision.state == "ON_HOLD"

        registry.release("US", s.source_id, "原因を解消した")  # 明示解除 → それ以前の失敗は数えない
        c = runs.claim("US", s.source_id)
        assert runs.finish(c, "FAILED") is None  # 解除後1回目の失敗では保留されない
        with app_role_factory.get_session("US") as conn, conn.begin():
            assert [e.event_type for e in conn.execute(text("SELECT event_type FROM acquisition_source_event WHERE source_id = :id ORDER BY event_id"), {"id": s.source_id})][-1] == "RELEASE"
    finally:
        set_config(app_role_factory, "US", "SELLER", retry_cooldown_minutes=60)


def test_status_change_participates_in_the_claim_lock(app_role_factory, registry, runs):
    """保留への変更がコミットされる前にclaimが来ても、変更の確定を待ち、確定後の状態で判定する。"""
    s = new_source(registry)
    result: list = []
    with app_role_factory.get_session("US") as conn, conn.begin():
        registry._locked_source(conn, "US", s.source_id)  # 識別ロック+行ロックを保持（保留設定の途中）
        t = threading.Thread(target=lambda: result.append(_try_claim(runs, s.source_id)))
        t.start()
        time.sleep(0.7)
        assert not result  # claimは待っている
        conn.execute(text("UPDATE acquisition_source SET status = 'ON_HOLD', hold_reason = 'MANUAL', hold_detail = 'x', held_at = now() WHERE source_id = :id"),
                     {"id": s.source_id})
    t.join(10)
    assert result == ["ON_HOLD"]  # 確定後の状態で再判定された


def _try_claim(runs, source_id):
    try:
        runs.claim("US", source_id)
        return "claimed"
    except NotEligible as e:
        return e.decision.state


def test_hold_exclude_release_interval_require_reasons_and_leave_history(app_role_factory, registry, runs):
    s = new_source(registry)
    for call in (lambda: registry.set_status("US", s.source_id, "ON_HOLD", " "), lambda: registry.release("US", s.source_id, "x"),
                 lambda: registry.set_interval("US", s.source_id, 7, "")):
        with pytest.raises(ValueError):
            call()
    registry.set_status("US", s.source_id, "EXCLUDED", "競合のため")
    with pytest.raises(NotEligible) as e:
        runs.claim("US", s.source_id)
    assert e.value.decision.state == "EXCLUDED"
    registry.release("US", s.source_id, "再開")
    registry.set_interval("US", s.source_id, 7, "短くする")
    with app_role_factory.get_session("US") as conn, conn.begin():
        events = conn.execute(text("SELECT event_type, detail, actor FROM acquisition_source_event WHERE source_id = :id ORDER BY event_id"),
                              {"id": s.source_id}).all()
    assert [e.event_type for e in events] == ["REGISTERED", "EXCLUDE", "RELEASE", "INTERVAL_SET"]
    assert all(e.actor == "tester" for e in events) and "競合のため" in events[1].detail


def test_seller_reads_storefront_blocked_seller_at_claim_time(app_role_factory, registry, runs):
    key = unique_key("ABLK")
    s = registry.register_source("US", "SELLER", key)
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("INSERT INTO storefront_blocked_seller (seller_id, reason) VALUES (:s, 'RETRY_LATER')"), {"s": key})
    with pytest.raises(NotEligible) as e:
        runs.claim("US", s.source_id)
    assert e.value.decision.state == "ON_HOLD"
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("UPDATE storefront_blocked_seller SET reason = 'PERMANENTLY_EXCLUDED' WHERE seller_id = :s"), {"s": key})
    with pytest.raises(NotEligible) as e:
        runs.claim("US", s.source_id)
    assert e.value.decision.state == "EXCLUDED"
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("DELETE FROM storefront_blocked_seller WHERE seller_id = :s"), {"s": key})
    runs.claim("US", s.source_id)  # 他の種別(キーワード)には影響しない/解除後は可
