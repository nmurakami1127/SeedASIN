"""旧証拠(legacy evidence)の結合テスト（実PostgreSQL、使い捨てtest-db）。

適用・分類・確定・却下・再開、REVIEW_LEGACYのplan/claim両方での保留、確定の競合と途中失敗からの
再実行、判断履歴の保持、読み取り専用のdry-runを検証する。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from acquisition.legacy import LegacyError, LegacyService
from acquisition.legacy_scan import (
    Observation, collect_collector_runs, collect_status_csv, collect_tool_runs, group_id, scan_done_dir, write_dry_run_csv,
)
from acquisition.lease import NotEligible
from acquisition.locks import identity_lock
from acquisition.normalize import key_hash, normalize_key
from acquisition.plan import build_plan
from acquisition.registry import SourceRegistry
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import make_active_report, make_runs, unique_key

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


@pytest.fixture()
def legacy(app_role_factory):
    return LegacyService(app_role_factory, actor="tester")


def obs(raw: str, *, days_ago: float = 5, typ: str | None = "SELLER", kind: str = "TOOL_RUN", market_grade: str = "UNCONFIRMED",
        market_hint: str | None = None, success: str = "ESTIMATE", type_grade: str = "ESTIMATE", qualifier: str = "") -> Observation:
    ref = "US:" + uuid.uuid4().hex[:10]
    return Observation(
        group=group_id(kind, ref), kind=kind, ref=ref, data_marketplace="US", raw_value=raw,
        evidence_at=datetime.now(timezone.utc) - timedelta(days=days_ago), evidence_at_basis="test", asin_count=10,
        success_grade=success, market_grade=market_grade, type_grade=type_grade, market_hint=market_hint,
        market_hint_basis="URLドメイン" if market_hint else None, type_hint=typ, type_hint_basis="test" if typ else None,
        proposed_primary=raw if typ else None, proposed_qualifier=qualifier,
    )


def apply(legacy, *observations, classify="all", confirm=False):
    return legacy.apply(observations, {o.group for o in observations}, classify=classify,
                        confirm_recorded_market=confirm, reason="テスト適用", batch_id="t")


def states(legacy, o):
    return {p: s.market_state for p, s in legacy.group_states(o.group).items()}


def test_unknown_market_evidence_is_copied_to_both_partitions_and_holds_both_markets(app_role_factory, registry, runs, legacy, tmp_path):
    key = unique_key("AHOLD")
    o = obs(key, days_ago=5)
    stats = apply(legacy, o)
    assert (stats["observations"], stats["classified"]) == (2, 2)
    assert states(legacy, o) == {"US": "UNKNOWN", "JP": "UNKNOWN"}
    us, jp = (registry.register_source(m, "SELLER", key) for m in ("US", "JP"))
    for m, s in (("US", us), ("JP", jp)):  # 取得先市場がどちらでも、claimで同じ保留が効く
        with pytest.raises(NotEligible) as e:
            runs.claim(m, s.source_id)
        assert e.value.decision.state == "REVIEW_LEGACY" and "legacy-confirm" in e.value.decision.blockers[0].detail
    # planでも同じ状態（実行不可として理由付き）
    rid = make_active_report(app_role_factory, tmp_path, "US", [("B0LEGACYA1", 1, 1.0, 1)])
    plan = build_plan(app_role_factory, ReportImportService(app_role_factory, None), "JP", [rid])
    assert any(f.source_id == jp.source_id and d.state == "REVIEW_LEGACY" for f, d in plan.blocked)


def test_other_type_same_string_and_unclassified_evidence_hold_nothing(registry, runs, legacy):
    word = unique_key("brush pen ")
    o = obs(word, typ="KEYWORD", days_ago=3)
    apply(legacy, o)
    # 同じ文字列でも別種別（ブランド名）の出典は保留されない
    brand = registry.register_source("US", "BRAND_NAME", word)
    runs.claim("US", brand.source_id)
    # 同じ種別・同じ文字列は保留される
    kw = registry.register_source("US", "KEYWORD", word)
    with pytest.raises(NotEligible):
        runs.claim("US", kw.source_id)
    # 未分類の証拠は種別を推測しない。値が一致しない出典は保留せず、一致する出典（種別を問わない）だけ確認対象として止める
    word2 = unique_key("unclassified ")
    u = obs(word2, typ=None, days_ago=3)
    apply(legacy, u)
    assert any(g["group"] == u.group and not g["state"].classified for g in legacy.list_groups())
    runs.claim("US", registry.register_source("US", "KEYWORD", unique_key("unrelated ")).source_id)
    for typ in ("KEYWORD", "BRAND_NAME"):
        src = registry.register_source("US", typ, word2.upper())  # 表記違い（大文字）でも正規化後に一致
        with pytest.raises(NotEligible) as e:
            runs.claim("US", src.source_id)
        assert e.value.decision.state == "REVIEW_LEGACY" and "種別が未確定" in e.value.decision.blockers[0].detail
    legacy.dismiss(u.group, reason="取得の証拠ではない")
    runs.claim("US", registry.register_source("US", "KEYWORD", word2).source_id)  # 人が却下すれば止まらない


def test_hold_expires_after_the_interval_but_the_evidence_is_kept(app_role_factory, registry, runs, legacy):
    key = unique_key("AOLDEV")
    o = obs(key, days_ago=31)
    apply(legacy, o)
    s = registry.register_source("US", "SELLER", key)
    runs.claim("US", s.source_id)  # 期限切れの旧証拠は保留しない
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM acquisition_legacy_evidence WHERE evidence_group = CAST(:g AS uuid)"),
                            {"g": o.group}).scalar_one() == 2  # 観測1 + 分類1。削除されず履歴として保持


def test_apply_is_selective_idempotent_and_skips_unconfirmed(legacy):
    a, b = obs(unique_key("AA")), obs(unique_key("AB"))
    bad = obs(unique_key("AC"), success="UNCONFIRMED")
    stats = legacy.apply([a, b, bad], {a.group, bad.group}, classify="none", confirm_recorded_market=False, reason="r", batch_id="t")
    assert (stats["selected"], stats["skipped_unconfirmed"], stats["observations"], stats["classified"]) == (2, 1, 2, 0)
    assert legacy.group_states(b.group) == {}  # 選択されていない行は適用されない
    again = legacy.apply([a], {a.group}, classify="none", confirm_recorded_market=False, reason="r", batch_id="t")
    assert again["observations"] == 0  # 二重に増えない
    assert not legacy.group_states(a.group)["US"].classified  # classify=noneなら提案を採用しない
    legacy.apply([a], {a.group}, classify="fact", confirm_recorded_market=False, reason="r", batch_id="t")
    assert not legacy.group_states(a.group)["US"].classified  # type_grade=ESTIMATE はfactでは採用しない
    legacy.apply([a], {a.group}, classify="all", confirm_recorded_market=False, reason="r", batch_id="t")
    assert legacy.group_states(a.group)["US"].classified


def test_confirm_releases_the_other_market_and_counts_as_reference_evidence(registry, runs, legacy):
    key = unique_key("ACONF")
    o = obs(key, days_ago=3)
    apply(legacy, o)
    assert legacy.confirm(o.group, "JP", "JPのストアフロントで取得したと確認") == ["JP", "US"]
    assert states(legacy, o) == {"US": "CONFIRMED_OTHER", "JP": "CONFIRMED_THIS"}
    us, jp = registry.register_source("US", "SELLER", key), registry.register_source("JP", "SELLER", key)
    runs.claim("US", us.source_id)  # 他方の市場は解除（この証拠では保留されない）
    with pytest.raises(NotEligible) as e:  # 確定した市場は、旧証拠(参考)として取得間隔に数える
        runs.claim("JP", jp.source_id)
    d = e.value.decision
    assert d.state == "TOO_RECENT" and d.gate_basis == "LEGACY" and d.last_success_at is None  # 新方式の成功とは区別
    assert legacy.confirm(o.group, "JP", "再実行") == []  # 冪等
    with pytest.raises(LegacyError, match="確定済み"):
        legacy.confirm(o.group, "US", "別市場への確定は拒否")


def test_decision_history_is_kept_with_reason_actor_and_time(app_role_factory, legacy):
    o = obs(unique_key("AHIS"), typ=None)
    apply(legacy, o, classify="none")
    legacy.classify(o.group, "SELLER", o.raw_value, "", "ID形式のためセラーと判断")
    legacy.confirm(o.group, "US", "USで確認")
    legacy.reopen(o.group, "確認が誤りだった")
    legacy.confirm(o.group, "JP", "JPだった")
    legacy.dismiss(o.group, "やはり取得の証拠ではない")
    with app_role_factory.get_session("US") as conn, conn.begin():
        rows = conn.execute(text("SELECT row_type, decision_type, target_marketplace, reason, actor, recorded_at FROM acquisition_legacy_evidence "
                                 "WHERE evidence_group = CAST(:g AS uuid) ORDER BY evidence_id"), {"g": o.group}).all()
    assert [(r.row_type, r.decision_type) for r in rows] == [
        ("OBSERVATION", None), ("DECISION", "CLASSIFY"), ("DECISION", "CONFIRM"), ("DECISION", "REOPEN"),
        ("DECISION", "CONFIRM"), ("DECISION", "DISMISS")]
    assert all(r.actor == "tester" and r.recorded_at is not None for r in rows)
    assert rows[2].reason == "USで確認" and rows[3].reason == "確認が誤りだった"  # 上書きされず全て残る
    assert states(legacy, o) == {"US": "DISMISSED", "JP": "DISMISSED"}
    with pytest.raises(LegacyError, match="reopen"):
        legacy.confirm(o.group, "US", "却下後の確定は拒否")
    for call in (lambda: legacy.confirm(o.group, "US", ""), lambda: legacy.dismiss(o.group, " "), lambda: legacy.classify(o.group, "SELLER", "x", "", "")):
        with pytest.raises(ValueError, match="理由"):
            call()


def test_evidence_rows_are_append_only_and_constraints_hold(app_role_factory, legacy):
    o = obs(unique_key("AAPP"))
    apply(legacy, o)
    with app_role_factory.get_session("US") as conn:
        for sql in ("UPDATE acquisition_legacy_evidence SET raw_value = 'x'", "DELETE FROM acquisition_legacy_evidence"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(sql))
        for decision, params in (("DECISION without reason", {"dt": "DISMISS", "reason": None, "target": None}),
                                 ("CONFIRM without target", {"dt": "CONFIRM", "reason": "r", "target": None})):
            with pytest.raises(IntegrityError, match="ck_ale"):
                with conn.begin():
                    conn.execute(text("INSERT INTO acquisition_legacy_evidence (evidence_group, marketplace, row_type, decision_type, "
                                      "reason, target_marketplace, actor) VALUES (CAST(:g AS uuid), 'US', 'DECISION', :dt, :reason, :target, 'x')"),
                                 {"g": o.group, **params})


@pytest.mark.parametrize("round_", range(4))
def test_concurrent_confirms_never_produce_two_confirmed_this(legacy, app_role_factory, round_):
    o = obs(unique_key("ARACE"))
    apply(legacy, o)
    barrier = threading.Barrier(2)
    outcome: dict[str, str] = {}

    def go(market):
        barrier.wait()
        try:
            LegacyService(app_role_factory, actor=f"t-{market}").confirm(o.group, market, f"{market}で確認")
            outcome[market] = "ok"
        except LegacyError:
            outcome[market] = "refused"

    ts = [threading.Thread(target=go, args=(m,)) for m in ("US", "JP")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcome.values()) == ["ok", "refused"]
    st = states(legacy, o)
    assert sorted(st.values()) == ["CONFIRMED_OTHER", "CONFIRMED_THIS"]  # 両方がCONFIRMED_THISにはならない


def test_failure_between_the_two_partitions_keeps_the_hold_and_rerun_repairs(registry, runs, app_role_factory):
    key = unique_key("AHALF")
    o = obs(key, days_ago=2)

    class Boom(Exception):
        pass

    def fail(point, group):
        if point == "after_target_confirm":
            raise Boom()

    LegacyService(app_role_factory, actor="t").apply([o], {o.group}, classify="all", confirm_recorded_market=False, reason="r", batch_id="t")
    with pytest.raises(Boom):
        LegacyService(app_role_factory, actor="t", hook=fail).confirm(o.group, "US", "途中で失敗する確定")
    half = {p: s.market_state for p, s in LegacyService(app_role_factory).group_states(o.group).items()}
    assert half == {"US": "CONFIRMED_THIS", "JP": "UNKNOWN"}  # 他方は未確定のまま＝保留が外れる方向には誤らない
    jp = registry.register_source("JP", "SELLER", key)
    with pytest.raises(NotEligible) as e:
        runs.claim("JP", jp.source_id)
    assert e.value.decision.state == "REVIEW_LEGACY"
    with pytest.raises(LegacyError, match="確定済み"):  # 途中状態で別市場への確定は拒否される
        LegacyService(app_role_factory).confirm(o.group, "JP", "競合する確定")
    assert LegacyService(app_role_factory, actor="t").confirm(o.group, "US", "再実行で補完") == ["JP"]
    assert {p: s.market_state for p, s in LegacyService(app_role_factory).group_states(o.group).items()} == {
        "US": "CONFIRMED_THIS", "JP": "CONFIRMED_OTHER"}
    runs.claim("JP", jp.source_id)


def test_decisions_take_the_same_identity_lock_as_claim(app_role_factory, legacy, registry):
    key = unique_key("ALOCK")
    o = obs(key, days_ago=1)
    apply(legacy, o)
    src = registry.register_source("US", "SELLER", key)
    kp, kq = normalize_key("SELLER", key)
    done: list = []
    with app_role_factory.get_session("US") as conn, conn.begin():
        identity_lock(conn, "US", "SELLER", key_hash("SELLER", kp, kq))  # claim等が識別ロックを保持している状態
        t = threading.Thread(target=lambda: done.append(legacy.confirm(o.group, "US", "確定")))
        t.start()
        time.sleep(0.8)
        assert not done  # 確定はclaimのロックの解放を待つ（直列化）
    t.join(10)
    assert done == [["US", "JP"]]
    assert src.source_id


def test_confirm_recorded_market_only_when_explicitly_chosen(app_role_factory, legacy):
    recorded = obs(unique_key("AREC"), kind="COLLECTOR_RUN", market_grade="FACT", market_hint="US", type_grade="FACT")
    no_flag = obs(unique_key("ANOF"), kind="COLLECTOR_RUN", market_grade="FACT", market_hint="US", type_grade="FACT")
    apply(legacy, no_flag, classify="fact", confirm=False)
    assert states(legacy, no_flag) == {"US": "UNKNOWN", "JP": "UNKNOWN"}  # 明示しなければ、記録で市場が分かっても確定しない
    stats = apply(legacy, recorded, classify="fact", confirm=True)
    assert states(legacy, recorded) == {"US": "CONFIRMED_THIS"} and stats["confirmed"] == 1  # 区画は市場側だけ
    row = legacy.group_states(recorded.group)["US"].decisions[-1]
    assert row["decision_type"] == "CONFIRM" and "旧証拠" in row["reason"] and "断定ではない" in row["reason"]


# ---------------------------------------------------------------- dry-run（読み取り専用）

def _seed_legacy_source_data(app_role_factory, market: str, seller: str, run_id: str, n_asins: int, source: str | None):
    with app_role_factory.get_session(market) as conn, conn.begin():
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES (:mp, :s, :r, :src)"),
                     {"mp": market, "s": seller, "r": run_id, "src": source})
        for i in range(n_asins):
            conn.execute(text("INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) VALUES (:mp, :r, :a, now() - interval '2 days')"),
                         {"mp": market, "r": run_id, "a": f"B0DRY{i:06d}"})


def test_dry_run_reads_only_and_grades_facts_estimates_and_unconfirmed(app_role_factory):
    seller, kw = unique_key("ADRY"), unique_key("dry keyword ")
    rid = uuid.uuid4().hex[:8]
    _seed_legacy_source_data(app_role_factory, "US", seller, "DR1" + rid, 3, "SELLER_PAGE")
    _seed_legacy_source_data(app_role_factory, "US", kw, "DR2" + rid, 2, "KEYWORD")
    _seed_legacy_source_data(app_role_factory, "US", "Some Brand Name", "DR3" + rid, 1, "LEGACY_IMPORT")
    _seed_legacy_source_data(app_role_factory, "US", unique_key("ANOOUT"), "DR4" + rid, 0, None)  # 取込なし
    with app_role_factory.get_session("US") as conn, conn.begin():
        legacy_run = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, asin_count, completed_at) VALUES "
            "('US','BRAND_PAGE','https://www.amazon.com/s?i=merchant-items&me=ACRUN" + rid.upper() + "','COMPLETED', 5, now()) RETURNING id")).scalar_one()
        zero = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status, asin_count, completed_at) VALUES "
            "('US','BRAND_PAGE','https://www.amazon.com/s?k=zero+result','COMPLETED', 0, now()) RETURNING id")).scalar_one()
        running = conn.execute(text(
            "INSERT INTO collector_run (marketplace, run_type, target, status) VALUES ('US','BRAND_PAGE','https://www.amazon.com/s?k=still-running','RUNNING') RETURNING id")).scalar_one()
        before = conn.execute(text("SELECT (SELECT count(*) FROM tool_execution_link), (SELECT count(*) FROM collector_run), (SELECT count(*) FROM tool_output_asin)")).one()

    tool = {o.raw_value: o for o in collect_tool_runs(app_role_factory, "US") if o.ref.split(":")[1].endswith(rid)}
    a, b, c = tool[seller], tool[kw], tool["Some Brand Name"]
    d = next(o for o in collect_tool_runs(app_role_factory, "US") if o.ref.endswith("DR4" + rid))
    assert (a.success_grade, a.market_grade, a.type_grade, a.type_hint) == ("ESTIMATE", "UNCONFIRMED", "FACT", "SELLER")
    assert (b.type_grade, b.type_hint) == ("FACT", "KEYWORD")
    assert (c.type_grade, c.type_hint) == ("UNCONFIRMED", None)  # 種別不明の文字列は分類しない
    assert d.success_grade == "UNCONFIRMED"  # 取込なしは証拠にしない
    assert a.asin_count == 3 and "acquired_at" in a.evidence_at_basis and "断定しない" in a.evidence_at_basis

    runs_obs, stats = collect_collector_runs(app_role_factory, "US")
    byref = {o.ref: o for o in runs_obs}
    ok, zr, rn = byref[f"US:{legacy_run}"], byref[f"US:{zero}"], byref[f"US:{running}"]
    assert (ok.market_grade, ok.market_hint, ok.success_grade, ok.type_hint) == ("FACT", "US", "ESTIMATE", "SELLER")  # 市場は事実、成功は推定
    assert (zr.success_grade, zr.type_hint) == ("UNCONFIRMED", "KEYWORD")  # 件数0は成功を断定しない
    assert rn.success_grade == "UNCONFIRMED" and stats["legacy_running_untouched"] >= 1  # 旧RUNNINGは証拠にしない

    with app_role_factory.get_session("US") as conn, conn.begin():
        after = conn.execute(text("SELECT (SELECT count(*) FROM tool_execution_link), (SELECT count(*) FROM collector_run), (SELECT count(*) FROM tool_output_asin)")).one()
        assert conn.execute(text("SELECT status FROM collector_run WHERE id = :id"), {"id": running}).scalar_one() == "RUNNING"
    assert tuple(before) == tuple(after)  # 何も変更していない


def test_dry_run_connection_is_read_only(app_role_factory):
    from acquisition import legacy_scan

    with app_role_factory.get_session("US") as conn, conn.begin():
        legacy_scan._set_read_only(conn)
        with pytest.raises(DBAPIError, match="read-only"):
            conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id) VALUES ('US','X','RO1')"))


def test_status_csv_done_dir_and_dry_run_csv(tmp_path: Path, app_role_factory):
    batch = tmp_path / "_amazoncom_batches" / "batch_20261016_001726"
    batch.mkdir(parents=True)
    (batch / "status.csv").write_text(
        "status,url_index,url,message,count,time\n"
        "OK,1,https://www.amazon.com/s?i=merchant-items&me=ASELLERFULL1&rh=p_36%3A1-2,completed,1,2026-10-16T00:17:36\n"
        "OK,2,https://www.amazon.com/s?i=merchant-items&me=ASELLERFULL1&rh=p_36%3A3-4,completed,0,2026-10-16T00:18:03\n"
        "OK,1,https://www.amazon.com/s?i=merchant-items&me=ASELLERPART1&rh=p_36%3A1-2,completed,1,2026-10-16T01:00:00\n"
        "NG,2,https://www.amazon.com/s?i=merchant-items&me=ASELLERPART1&rh=p_36%3A3-4,failed,0,2026-10-16T01:01:00\n"
        "NG,1,https://www.amazon.com/s?i=merchant-items&me=ASELLERNOOK1,failed,0,2026-10-16T02:00:00\n",
        encoding="utf-8-sig")
    out = {o.raw_value: o for o in collect_status_csv(tmp_path)}
    assert set(out) == {"ASELLERFULL1", "ASELLERPART1"}  # OKが1件も無いセラーは対象外
    full, part = out["ASELLERFULL1"], out["ASELLERPART1"]
    assert (full.success_grade, full.market_grade, full.market_hint, full.type_hint) == ("ESTIMATE", "FACT", "US", "SELLER")
    assert part.success_grade == "UNCONFIRMED"  # NGを含む日は未確認
    assert full.evidence_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-10-15 15:18"  # ローカル(JST)時刻として解釈
    assert "JST" in full.evidence_at_basis

    done = tmp_path / "done"
    done.mkdir()
    (done / "20261008-01-keywords_us.csv").write_text("category_alias,keyword\n,aaa\noffice,bbb\n", encoding="utf-8")
    (done / "20261013-01-keywords_us.csv").write_text("category_alias,keyword\n,aaa\n", encoding="utf-8")
    s = scan_done_dir(done)
    assert (s["files"], s["keyword_rows"], s["distinct"], s["repeated"]) == (2, 3, 2, 1) and "証拠にしない" in s["note"]

    csv_path = write_dry_run_csv(list(out.values()), tmp_path / "dry.csv")
    header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert header[0] == "apply" and {"success_grade", "market_grade", "type_grade", "proposed_action", "within_30d"} <= set(header)
    from acquisition.legacy import read_selected_groups

    assert read_selected_groups(csv_path) == set()  # 利用者が選択するまで何も適用されない

# ---------------------------------------------------------------- 最終レビュー: ロックの解放・競合・決定性

def _advisory_lock_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted")).scalar_one()


@pytest.fixture()
def owner_engine(superuser_url):
    from sqlalchemy import create_engine

    engine = create_engine(superuser_url)
    yield engine
    engine.dispose()


def test_group_lock_is_released_on_the_same_connection_even_when_the_body_raises(owner_engine, app_role_factory):
    class Boom(Exception):
        pass

    base = _advisory_lock_count(owner_engine)
    last = None
    for i in range(6):  # プールの接続を繰り返し再利用してもロックが残らない
        o = obs(unique_key(f"ALK{i}"), days_ago=2)
        last = o
        LegacyService(app_role_factory).apply([o], {o.group}, classify="all", confirm_recorded_market=False, reason="r", batch_id="t")

        def fail(point, group):
            raise Boom()

        with pytest.raises(Boom):
            LegacyService(app_role_factory, hook=fail).confirm(o.group, "US", "途中で失敗")
        assert _advisory_lock_count(owner_engine) == base  # 例外後にロックが残っていない
    probe = LegacyService(app_role_factory)
    with probe._group_lock(last.group):  # 同じ群のロックを、別の呼び出しがすぐ取得できる
        assert _advisory_lock_count(owner_engine) == base + 1
    assert _advisory_lock_count(owner_engine) == base


def test_group_lock_discards_a_connection_whose_lock_cannot_be_confirmed_released(owner_engine, app_role_factory):
    """接続が切れて解放を確認できなくても例外を増やさず、その接続はプールへ戻さない。ロックは残らない。"""
    svc = LegacyService(app_role_factory)
    base = _advisory_lock_count(owner_engine)
    group = str(uuid.uuid4())
    with svc._group_lock(group):
        with owner_engine.connect() as conn:
            pid = conn.execute(text("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted ORDER BY pid DESC LIMIT 1")).scalar_one()
            conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            conn.commit()
        time.sleep(0.5)
    assert _advisory_lock_count(owner_engine) == base  # サーバー側で解放済み。残らない
    with svc._group_lock(group):  # 破棄された接続が再利用されず、新しい接続で正常に取得できる
        assert _advisory_lock_count(owner_engine) == base + 1
    assert _advisory_lock_count(owner_engine) == base
    o = obs(unique_key("AAFTER"))
    apply(LegacyService(app_role_factory), o)  # プールが正常
    assert states(LegacyService(app_role_factory), o) == {"US": "UNKNOWN", "JP": "UNKNOWN"}


def test_after_a_half_done_confirm_the_opposite_market_cannot_succeed_even_bypassing_the_group_lock(app_role_factory):
    o = obs(unique_key("AREV"), days_ago=2)
    LegacyService(app_role_factory).apply([o], {o.group}, classify="all", confirm_recorded_market=False, reason="r", batch_id="t")

    def fail(point, group):
        if point == "after_target_confirm":
            raise RuntimeError("停止")

    with pytest.raises(RuntimeError):
        LegacyService(app_role_factory, hook=fail).confirm(o.group, "US", "USへ確定（途中で停止）")
    svc = LegacyService(app_role_factory)
    with pytest.raises(LegacyError, match="確定済み"):
        svc.confirm(o.group, "JP", "逆の市場への確定")  # 通常経路
    # 群ロックを迂回して区画へ直接追記しようとしても、別市場への確定は成功しない（二重防御）
    with pytest.raises(LegacyError, match="reopen"):
        svc._append("US", o.group, "CONFIRM", "迂回", target="JP")
    assert {p: s.market_state for p, s in svc.group_states(o.group).items()} == {"US": "CONFIRMED_THIS", "JP": "UNKNOWN"}
    svc.reopen(o.group, "いったん戻す")  # 明示的にreopenすれば別市場へ確定できる（履歴は残る）
    assert svc.confirm(o.group, "JP", "JPへ確定") == ["JP", "US"]


def test_latest_state_is_deterministic_for_decisions_with_the_same_timestamp(app_role_factory, legacy):
    """同一トランザクション内（recorded_atが同値）の判断でも、evidence_idの順で最新が決まる。"""
    o = obs(unique_key("ATIE"))
    apply(legacy, o)
    with app_role_factory.get_session("US") as conn, conn.begin():
        for dt, target in (("CONFIRM", "US"), ("DISMISS", None), ("REOPEN", None), ("DISMISS", None)):
            conn.execute(text("INSERT INTO acquisition_legacy_evidence (evidence_group, marketplace, row_type, decision_type, "
                              "target_marketplace, reason, actor) VALUES (CAST(:g AS uuid), 'US', 'DECISION', :dt, :t, 'same-ts', 'x')"),
                         {"g": o.group, "dt": dt, "t": target})
        stamps = {r[0] for r in conn.execute(text("SELECT recorded_at FROM acquisition_legacy_evidence WHERE evidence_group = CAST(:g AS uuid) AND actor = 'x'"), {"g": o.group})}
    assert len(stamps) == 1  # 全て同時刻
    for _ in range(3):  # 何度読み直しても同じ最新状態
        assert legacy.group_states(o.group)["US"].market_state == "DISMISSED"


def test_applying_a_selection_writes_exactly_the_planned_rows_and_unknown_market_holds_both(app_role_factory, registry, runs, legacy):
    """選択結果の予定行数（SELECT_BOTH=4行、SELECT_RECORDED_MARKET=3行）が、実際の適用で書かれる行数と一致する。"""
    from acquisition.legacy_scan import select_for_migration

    both = obs(unique_key("SELBOTH"), typ="KEYWORD", type_grade="FACT", days_ago=4)
    rec = obs(unique_key("ASELREC"), kind="COLLECTOR_RUN", typ="SELLER", type_grade="FACT", market_grade="FACT", market_hint="JP", days_ago=4)
    heur = obs(unique_key("ASELHEUR"), typ="SELLER", type_grade="ESTIMATE", days_ago=4)
    rows = select_for_migration([both, rec, heur], lambda t, m: 30, datetime.now(timezone.utc))
    assert [r.decision for r in rows] == ["SELECT_BOTH", "SELECT_RECORDED_MARKET", "EXCLUDE"]
    planned = sum(r.planned_rows for r in rows)
    selected = {r.obs.group for r in rows if r.selected}

    def evidence_rows(group):
        total = 0
        for m in ("US", "JP"):
            with app_role_factory.get_session(m) as conn, conn.begin():
                total += conn.execute(text("SELECT count(*) FROM acquisition_legacy_evidence WHERE evidence_group = CAST(:g AS uuid)"),
                                      {"g": group}).scalar_one()
        return total

    legacy.apply([both, rec, heur], selected, classify="fact", confirm_recorded_market=True, reason="選択結果の適用", batch_id="t")
    assert evidence_rows(both.group) == 4 and evidence_rows(rec.group) == 3 and evidence_rows(heur.group) == 0
    assert evidence_rows(both.group) + evidence_rows(rec.group) == planned  # 予定行数と実際の行数が一致
    # 市場不明・種別確認済みの証拠は、両市場でREVIEW_LEGACY。成功も取得先市場も確定していない
    assert states(legacy, both) == {"US": "UNKNOWN", "JP": "UNKNOWN"}
    kw_us, kw_jp = (registry.register_source(m, "KEYWORD", both.raw_value) for m in ("US", "JP"))
    for m, s in (("US", kw_us), ("JP", kw_jp)):
        with pytest.raises(NotEligible) as e:
            runs.claim(m, s.source_id)
        assert e.value.decision.state == "REVIEW_LEGACY"
    # 記録で市場が分かる参考証拠は、その市場でだけ取得間隔に数える（新方式の成功ではない）。他方の市場には影響しない
    sel_jp, sel_us = (registry.register_source(m, "SELLER", rec.raw_value) for m in ("JP", "US"))
    with pytest.raises(NotEligible) as e:
        runs.claim("JP", sel_jp.source_id)
    d = e.value.decision
    assert d.state == "TOO_RECENT" and d.gate_basis == "LEGACY" and d.last_success_at is None
    runs.claim("US", sel_us.source_id)
    # 推定のみの証拠は適用されていないので、どの出典も保留しない
    runs.claim("US", registry.register_source("US", "SELLER", heur.raw_value).source_id)


def test_observe_only_history_is_counted_as_unconfirmed_and_holds_nothing_and_rerun_is_safe(registry, runs, legacy):
    key = unique_key("AOBSERVE")
    o = obs(key, days_ago=3, type_grade="ESTIMATE")  # ID形式の推定のみ: 分類せず観測として残す
    before = {m: legacy.unclassified_count(m) for m in ("US", "JP")}
    first = legacy.apply([o], {o.group}, classify="fact", confirm_recorded_market=False, reason="r", batch_id="t")
    assert (first["observations"], first["classified"]) == (2, 0)
    assert {m: legacy.unclassified_count(m) for m in ("US", "JP")} == {m: before[m] + 1 for m in before}
    src = registry.register_source("US", "SELLER", key)
    with pytest.raises(NotEligible) as e:  # 同じ値の未確認履歴がある出典は、人が分類/却下するまで自動選択しない
        runs.claim("US", src.source_id)
    assert "種別が未確定" in e.value.decision.blockers[0].detail
    again = legacy.apply([o], {o.group}, classify="fact", confirm_recorded_market=False, reason="r", batch_id="t")
    assert again["observations"] == 0  # 再実行しても重複しない
    legacy.classify(o.group, "SELLER", key, "", reason="人が確認")
    assert legacy.unclassified_count("US") == before["US"]  # 人が分類すると未確認から外れる
    with pytest.raises(NotEligible) as e2:  # 分類後は市場未確定の旧証拠として両市場REVIEW_LEGACY
        runs.claim("US", src.source_id)
    assert "取得先市場が未確定" in e2.value.decision.blockers[0].detail


def test_apply_rejects_overlong_fields_before_writing_anything(legacy):
    ok, bad = obs(unique_key("AOK")), obs(unique_key("ABAD"))
    bad.type_hint_basis = "x" * 61  # 実DB投入中に起きた「列長超過」の再発防止: 全件を先に検査して1行も書かない
    with pytest.raises(ValueError, match="列の長さ"):
        legacy.apply([ok, bad], {ok.group, bad.group}, classify="all", confirm_recorded_market=False, reason="r", batch_id="t")
    assert legacy.group_states(ok.group) == {} and legacy.group_states(bad.group) == {}


def test_apply_cli_refuses_when_planned_fact_cannot_be_reproduced(tmp_path, monkeypatch):
    from acquisition.legacy import read_plan_type_grades

    plan = tmp_path / "plan.csv"
    plan.write_text("apply,group,type_grade\nY,g1,FACT(確認済み)\nY,g2,ESTIMATE(推定)\n,g3,FACT(確認済み)\n", encoding="utf-8-sig")
    assert read_plan_type_grades(plan) == {"g1": "FACT", "g2": "ESTIMATE"}  # 台帳指定の付け忘れ検出に使う（選択行のみ）
