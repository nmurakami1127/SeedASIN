"""ACTIVEレポートの無効化(deactivate)の結合テスト（実PostgreSQL、使い捨てtest-db）。"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from origin_selection.candidates import CandidateSelectionError, fetch_candidates, select_import
from origin_selection.report_import import ActivationConflict, ActivationRefused, ReportImportService
from tests.integration.report_helpers import fixture_bytes, place, unique_period

pytestmark = pytest.mark.integration

_US_HEADER = (
    "（子）ASIN,セッション数 - 合計,セッション率 - 合計,おすすめ出品（おすすめ商品）の獲得率,"
    "注文された商品点数,ユニットセッション率,注文商品の売上額,注文品目総数"
)


@pytest.fixture()
def service(app_role_factory, kpi_report_factory) -> ReportImportService:
    return ReportImportService(app_role_factory, kpi_report_factory, actor="tester")


def _csv(*rows: str) -> bytes:
    return "\n".join([_US_HEADER, *rows]).encode("utf-8")


def _stage(service, tmp_path, name, content, period, market="US"):
    res, _ = place(tmp_path, market, name, content)
    return service.import_file(res, *period).report


def _activate(service, rid, mode, mp="US"):
    return service.activate(rid, mp, mode, service.preview_activation(rid, mp, mode), "t")


def _deactivate(service, rid, reason="期間の指定誤り", mp="US"):
    return service.deactivate(rid, mp, reason, service.preview_deactivation(rid, mp))


def _active_count(factory, period, mp="US"):
    with factory.get_session(mp) as conn, conn.begin():
        return conn.execute(
            text("SELECT count(*) FROM business_report_import WHERE period_start=:p AND status='ACTIVE'"), {"p": period[0]}
        ).scalar_one()


def test_deactivate_marks_superseded_with_event_reason_actor_and_time(tmp_path, service, app_role_factory):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    _activate(service, r.import_id, "first")
    updated = _deactivate(service, r.import_id, "  期間の指定誤り  ")
    assert updated.status == "SUPERSEDED"

    ev = service.get_events(r.import_id, "US")[-1]
    assert ev.event_type == "DEACTIVATED"  # 差替えによる旧版化(SUPERSEDEDイベント)とは別の種別
    assert ev.note == "期間の指定誤り" and ev.actor == "tester" and ev.occurred_at is not None
    assert ev.related_import_id is None and ev.activation_mode is None
    assert [e.event_type for e in service.get_events(r.import_id, "US")] == ["IMPORTED", "ACTIVATED", "DEACTIVATED"]


def test_deactivate_keeps_rows_and_history_and_activates_nothing_else(tmp_path, service, app_role_factory):
    period = unique_period()
    old = _stage(service, tmp_path, "v1.csv", _csv("B0OLDOLD01,10,0%,10%,5,0%,$0,5"), period)
    new = _stage(service, tmp_path, "v2.csv", _csv("B0NEWNEW01,10,0%,10%,2,0%,$0,2"), period)
    _activate(service, old.import_id, "first")
    _activate(service, new.import_id, "replace")  # old は差替えで旧版

    _deactivate(service, new.import_id)
    with app_role_factory.get_session("US") as conn, conn.begin():
        rows = {
            i: conn.execute(text("SELECT count(*) FROM business_report_row WHERE import_id=:i"), {"i": i}).scalar_one()
            for i in (old.import_id, new.import_id)
        }
        hist = conn.execute(
            text("SELECT count(*) FROM import_run_history WHERE import_run_id = ANY(:r)"),
            {"r": [old.import_run_id, new.import_run_id]},
        ).scalar_one()
        statuses = dict(conn.execute(text("SELECT import_id, status FROM business_report_import WHERE period_start=:p"), {"p": period[0]}).all())
    assert rows == {old.import_id: 1, new.import_id: 1}  # 元行は削除されない
    assert hist == 2
    # 過去のレポートは自動で有効化されない: その期間に有効版が無い状態を許す
    assert statuses == {old.import_id: "SUPERSEDED", new.import_id: "SUPERSEDED"}
    assert _active_count(app_role_factory, period) == 0


def test_deactivated_report_is_excluded_from_normal_candidates(tmp_path, service, app_role_factory):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    _activate(service, r.import_id, "first")
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert select_import(conn, "US", period_start=period[0], period_end=period[1]).import_id == r.import_id
    _deactivate(service, r.import_id)
    with app_role_factory.get_session("US") as conn, conn.begin():
        # --import-idで明示しても通常の候補出力対象にならない
        with pytest.raises(CandidateSelectionError, match="有効版ではありません"):
            select_import(conn, "US", import_id=r.import_id)
        # 市場＋期間でも有効版が無い
        with pytest.raises(CandidateSelectionError, match="有効版がありません"):
            select_import(conn, "US", period_start=period[0], period_end=period[1])
        # 内容確認のための明示的な許可があるときだけ見える（通常出力ではない）
        info = select_import(conn, "US", import_id=r.import_id, allow_non_active=True)
        assert info.status == "SUPERSEDED" and len(fetch_candidates(conn, info.import_id)) == 7


def test_reason_is_required(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    _activate(service, r.import_id, "first")
    preview = service.preview_deactivation(r.import_id, "US")
    for bad in (None, "", "   "):
        with pytest.raises(ActivationRefused, match="理由"):
            service.deactivate(r.import_id, "US", bad, preview)
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT status FROM business_report_import WHERE import_id=:i"), {"i": r.import_id}).scalar_one() == "ACTIVE"
    # DBのCHECKでも理由なしのDEACTIVATEDイベントは入らない（アプリ層をすり抜けた場合）
    with app_role_factory.get_session("US") as conn:
        for note in (None, "  "):
            with pytest.raises(DBAPIError, match="ck_brie_deactivated_reason"):
                with conn.begin():
                    conn.execute(
                        text("INSERT INTO business_report_import_event (import_id, marketplace, event_type, note, actor) "
                             "VALUES (:i, 'US', 'DEACTIVATED', :n, 'x')"),
                        {"i": r.import_id, "n": note},
                    )


@pytest.mark.parametrize("prior", ["STAGED", "REJECTED", "SUPERSEDED"])
def test_only_active_can_be_deactivated(tmp_path, service, prior):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    if prior == "REJECTED":
        service.reject(r.import_id, "US", "x")
    if prior == "SUPERSEDED":
        _activate(service, r.import_id, "first")
        _deactivate(service, r.import_id)
    preview = service.preview_deactivation(r.import_id, "US")
    assert preview.blockers
    with pytest.raises(ActivationRefused, match="ACTIVE"):
        service.deactivate(r.import_id, "US", "理由", preview)


def test_restore_after_deactivation_with_first_mode(tmp_path, service, app_role_factory):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    _activate(service, r.import_id, "first")
    _deactivate(service, r.import_id, "誤操作かもしれない")
    res = _activate(service, r.import_id, "first")  # 有効版が無い期間では first で復元できる
    assert res.restored and res.superseded_import_id is None
    assert _active_count(app_role_factory, period) == 1
    assert [e.event_type for e in service.get_events(r.import_id, "US")][-2:] == ["DEACTIVATED", "RESTORED"]
    # 復元後は候補に戻る
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert select_import(conn, "US", period_start=period[0], period_end=period[1]).import_id == r.import_id


def test_restore_refused_when_another_version_became_active(tmp_path, service, app_role_factory):
    period = unique_period()
    a = _stage(service, tmp_path, "a.csv", _csv("B0AAAAAAA1,1,0%,1%,1,0%,$0,1"), period)
    b = _stage(service, tmp_path, "b.csv", _csv("B0BBBBBBB1,1,0%,1%,2,0%,$0,2"), period)
    _activate(service, a.import_id, "first")
    _deactivate(service, a.import_id)
    _activate(service, b.import_id, "first")  # 正しいレポートを有効化
    preview = service.preview_activation(a.import_id, "US", "first")
    assert any("replace" in x for x in preview.blockers)  # 復元はfirstではできず、差替え(replace)の明示が必要
    assert _active_count(app_role_factory, period) == 1
    res = _activate(service, a.import_id, "replace")  # 明示すれば戻せる
    assert res.restored and res.superseded_import_id == b.import_id


def test_deactivate_then_import_correct_period_then_activate(tmp_path, service, app_role_factory):
    """README手順: 誤ったACTIVE版を無効化→正しい期間で取込→確認→有効化。"""
    wrong, right = unique_period(), unique_period()
    content = fixture_bytes("synthetic_us.csv")
    bad = _stage(service, tmp_path, "a.csv", content, wrong)
    _activate(service, bad.import_id, "first")
    _deactivate(service, bad.import_id, "期間の指定誤り")
    good = _stage(service, tmp_path, "a.csv", content, right)  # 同じファイルでも期間が違えば別の取込
    assert good.import_id != bad.import_id
    _activate(service, good.import_id, "first")
    with app_role_factory.get_session("US") as conn, conn.begin():
        info = select_import(conn, "US", period_start=right[0], period_end=right[1])
        assert info.import_id == good.import_id
        with pytest.raises(CandidateSelectionError):
            select_import(conn, "US", period_start=wrong[0], period_end=wrong[1])


def test_stale_preview_conflicts_when_state_changed_meanwhile(tmp_path, service, app_role_factory):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    _activate(service, r.import_id, "first")
    preview = service.preview_deactivation(r.import_id, "US")  # 確認待ち…
    _deactivate(service, r.import_id, "別の操作が先に無効化")  # …別の操作が先に実行
    with pytest.raises(ActivationConflict, match="状態が変わりました"):
        service.deactivate(r.import_id, "US", "遅れて実行", preview)
    assert len([e for e in service.get_events(r.import_id, "US") if e.event_type == "DEACTIVATED"]) == 1


def test_deactivation_preview_holds_no_lock(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    _activate(service, r.import_id, "first")
    service.preview_deactivation(r.import_id, "US")
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("SELECT 1 FROM business_report_import WHERE import_id=:i FOR UPDATE NOWAIT"), {"i": r.import_id})
        assert conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")).scalar_one() == 0


def test_concurrent_deactivations_only_one_wins(tmp_path, service, app_role_factory):
    period = unique_period()
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), period)
    _activate(service, r.import_id, "first")
    previews = [service.preview_deactivation(r.import_id, "US") for _ in range(2)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(pv) -> None:
        barrier.wait()
        try:
            service.deactivate(r.import_id, "US", "同時実行", pv)
            outcomes.append("ok")
        except (ActivationConflict, ActivationRefused) as exc:
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=run, args=(pv,)) for pv in previews]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["ActivationConflict", "ok"]
    assert len([e for e in service.get_events(r.import_id, "US") if e.event_type == "DEACTIVATED"]) == 1


def test_concurrent_deactivate_and_replace_keep_invariants(tmp_path, service, app_role_factory):
    """無効化と差替えが競合しても、有効版が複数になったり状態が壊れたりしない。"""
    period = unique_period()
    a = _stage(service, tmp_path, "a.csv", _csv("B0AAAAAAA1,1,0%,1%,1,0%,$0,1"), period)
    b = _stage(service, tmp_path, "b.csv", _csv("B0BBBBBBB1,1,0%,1%,2,0%,$0,2"), period)
    _activate(service, a.import_id, "first")
    deact_preview = service.preview_deactivation(a.import_id, "US")
    repl_preview = service.preview_activation(b.import_id, "US", "replace")
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def do_deactivate() -> None:
        barrier.wait()
        try:
            service.deactivate(a.import_id, "US", "競合", deact_preview)
            outcomes["deactivate"] = "ok"
        except (ActivationConflict, ActivationRefused) as exc:
            outcomes["deactivate"] = type(exc).__name__

    def do_replace() -> None:
        barrier.wait()
        try:
            service.activate(b.import_id, "US", "replace", repl_preview)
            outcomes["replace"] = "ok"
        except (ActivationConflict, ActivationRefused) as exc:
            outcomes["replace"] = type(exc).__name__

    threads = [threading.Thread(target=do_deactivate), threading.Thread(target=do_replace)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # 少なくとも片方は検出されて中止される（同時に両方が成功して矛盾した状態にはならない）
    assert "ok" in outcomes.values() and any(v != "ok" for v in outcomes.values()), outcomes
    assert _active_count(app_role_factory, period) <= 1
    with app_role_factory.get_session("US") as conn, conn.begin():
        statuses = dict(conn.execute(text("SELECT import_id, status FROM business_report_import WHERE period_start=:p"), {"p": period[0]}).all())
    if outcomes["deactivate"] == "ok":
        assert statuses[a.import_id] == "SUPERSEDED" and statuses[b.import_id] == "STAGED"
    else:
        assert statuses[a.import_id] == "SUPERSEDED" and statuses[b.import_id] == "ACTIVE"


def test_rls_and_permissions_unchanged_for_deactivation(tmp_path, service, app_role_factory):
    us = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    jp = _stage(service, tmp_path, "j.csv", fixture_bytes("synthetic_jp.csv"), unique_period(), market="JP")
    _activate(service, us.import_id, "first")
    _activate(service, jp.import_id, "first", mp="JP")
    # 別市場のimportは、そのimportの市場のセッションからしか操作できない（RLS）
    with pytest.raises(ActivationRefused, match="存在しません"):
        service.preview_deactivation(jp.import_id, "US")
    _deactivate(service, us.import_id)
    assert service.get_report(jp.import_id, "JP").status == "ACTIVE"  # JP側は影響を受けない
    # 追記専用・列限定UPDATEは維持: eventの書換え・importの不変列更新は拒否される
    with app_role_factory.get_session("US") as conn:
        for sql in (
            "UPDATE business_report_import_event SET note = 'x'",
            "DELETE FROM business_report_import_event",
            "UPDATE business_report_import SET period_start = period_start",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                with conn.begin():
                    conn.execute(text(sql))
