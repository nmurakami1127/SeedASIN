"""ビジネスレポートの有効化・差替え・復元・不採用の結合テスト（実PostgreSQL、使い捨てtest-db）。"""
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
    preview = service.preview_activation(rid, mp, mode)
    return service.activate(rid, mp, mode, preview, "test")


def _status(app_role_factory, rid, mp="US"):
    with app_role_factory.get_session(mp) as conn, conn.begin():
        return conn.execute(text("SELECT status FROM business_report_import WHERE import_id=:i"), {"i": rid}).scalar_one()


def _events(app_role_factory, rid, mp="US"):
    with app_role_factory.get_session(mp) as conn, conn.begin():
        return [
            (e.event_type, e.activation_mode, e.related_import_id)
            for e in conn.execute(
                text("SELECT event_type, activation_mode, related_import_id FROM business_report_import_event "
                     "WHERE import_id=:i ORDER BY event_id"),
                {"i": rid},
            )
        ]


def test_import_never_auto_activates(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    assert _status(app_role_factory, r.import_id) == "STAGED"
    # 有効版が無いので、候補は有効版を要求すると取得できない
    with app_role_factory.get_session("US") as conn, conn.begin():
        with pytest.raises(CandidateSelectionError, match="有効版ではありません"):
            select_import(conn, "US", import_id=r.import_id)


def test_first_then_replace_keeps_old_version_and_single_active(tmp_path, service, app_role_factory):
    period = unique_period()
    old = _stage(service, tmp_path, "v1.csv", _csv("B0OLDOLD01,10,0%,10%,5,0%,$0,5"), period)
    new = _stage(service, tmp_path, "v2.csv", _csv("B0NEWNEW01,10,0%,10%,2,0%,$0,2"), period)

    res = _activate(service, old.import_id, "first")
    assert res.superseded_import_id is None
    # 有効版がある状態での初回登録(first)は拒否、差替えを促す
    preview = service.preview_activation(new.import_id, "US", "first")
    assert preview.blockers and any("replace" in b for b in preview.blockers)
    with pytest.raises(ActivationRefused):
        service.activate(new.import_id, "US", "first", preview)

    res = _activate(service, new.import_id, "replace")
    assert res.superseded_import_id == old.import_id and not res.restored
    assert _status(app_role_factory, old.import_id) == "SUPERSEDED"
    assert _status(app_role_factory, new.import_id) == "ACTIVE"

    # 旧版の行は削除されず履歴として残る
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM business_report_row WHERE import_id=:i"), {"i": old.import_id}).scalar_one() == 1
        actives = conn.execute(
            text("SELECT count(*) FROM business_report_import WHERE period_start=:p AND status='ACTIVE'"), {"p": period[0]}
        ).scalar_one()
    assert actives == 1
    assert _events(app_role_factory, old.import_id) == [("IMPORTED", None, None), ("ACTIVATED", "FIRST", None), ("SUPERSEDED", None, new.import_id)]
    assert _events(app_role_factory, new.import_id)[-1] == ("ACTIVATED", "REPLACE", old.import_id)


def test_no_double_counting_candidates_use_only_the_active_version(tmp_path, service, app_role_factory):
    period = unique_period()
    old = _stage(service, tmp_path, "v1.csv", _csv("B0SAMEASN1,10,0%,10%,5,0%,$0,5"), period)
    new = _stage(service, tmp_path, "v2.csv", _csv("B0SAMEASN1,12,0%,20%,7,0%,$0,7"), period)
    _activate(service, old.import_id, "first")
    _activate(service, new.import_id, "replace")
    with app_role_factory.get_session("US") as conn, conn.begin():
        info = select_import(conn, "US", period_start=period[0], period_end=period[1])
        cands = fetch_candidates(conn, info.import_id)
    assert info.import_id == new.import_id
    assert [(c.asin, c.units_sum, c.sessions_sum) for c in cands] == [("B0SAMEASN1", 7, 12)]  # 5+7=12にはならない


def test_restore_superseded_version(tmp_path, service, app_role_factory):
    period = unique_period()
    old = _stage(service, tmp_path, "v1.csv", _csv("B0OLDOLD01,10,0%,10%,5,0%,$0,5"), period)
    new = _stage(service, tmp_path, "v2.csv", _csv("B0NEWNEW01,10,0%,10%,2,0%,$0,2"), period)
    _activate(service, old.import_id, "first")
    _activate(service, new.import_id, "replace")
    res = _activate(service, old.import_id, "replace")
    assert res.restored and res.superseded_import_id == new.import_id
    assert (_status(app_role_factory, old.import_id), _status(app_role_factory, new.import_id)) == ("ACTIVE", "SUPERSEDED")
    assert _events(app_role_factory, old.import_id)[-1][0] == "RESTORED"


def test_replace_without_active_is_refused(tmp_path, service):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    preview = service.preview_activation(r.import_id, "US", "replace")
    assert any("first" in b for b in preview.blockers)
    with pytest.raises(ActivationRefused):
        service.activate(r.import_id, "US", "replace", preview)


def test_report_with_any_row_error_cannot_be_activated(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "e.csv", _csv("B0TESTUS01,1,0%,1%,1,0%,$0,1", "BAD,1,0%,1%,1,0%,$0,1"), unique_period())
    assert r.error_rows == 1
    preview = service.preview_activation(r.import_id, "US", "first")
    assert preview.has_error_rows and preview.blockers
    with pytest.raises(ActivationRefused, match="行エラー"):
        service.activate(r.import_id, "US", "first", preview)
    assert _status(app_role_factory, r.import_id) == "STAGED"
    # 不採用にはできる
    assert service.reject(r.import_id, "US", "行エラー").status == "REJECTED"


def test_stale_preview_is_rechecked_at_activation_start(tmp_path, service, app_role_factory):
    period = unique_period()
    a = _stage(service, tmp_path, "a.csv", _csv("B0AAAAAAA1,1,0%,1%,1,0%,$0,1"), period)
    b = _stage(service, tmp_path, "b.csv", _csv("B0BBBBBBB1,1,0%,1%,2,0%,$0,2"), period)
    preview_b = service.preview_activation(b.import_id, "US", "first")  # 確認待ちの間に…
    _activate(service, a.import_id, "first")  # …別の操作が先に有効化した
    with pytest.raises(ActivationConflict, match="状態が変わりました"):
        service.activate(b.import_id, "US", "first", preview_b)
    assert (_status(app_role_factory, a.import_id), _status(app_role_factory, b.import_id)) == ("ACTIVE", "STAGED")


def test_preview_holds_no_lock_or_open_transaction(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    service.preview_activation(r.import_id, "US", "first")
    # 確認待ち相当の間、別接続が対象行をNOWAITでロックできる（プレビューは何も保持していない）
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text("SELECT import_id FROM business_report_import WHERE import_id=:i FOR UPDATE NOWAIT"), {"i": r.import_id}
        )
        locks = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")).scalar_one()
    assert locks == 0


def test_concurrent_first_activations_yield_exactly_one_active(tmp_path, service, app_role_factory):
    period = unique_period()
    a = _stage(service, tmp_path, "a.csv", _csv("B0AAAAAAA1,1,0%,1%,1,0%,$0,1"), period)
    b = _stage(service, tmp_path, "b.csv", _csv("B0BBBBBBB1,1,0%,1%,2,0%,$0,2"), period)
    previews = {r.import_id: service.preview_activation(r.import_id, "US", "first") for r in (a, b)}
    barrier = threading.Barrier(2)
    outcomes: dict[int, str] = {}

    def run(rid: int) -> None:
        barrier.wait()
        try:
            service.activate(rid, "US", "first", previews[rid])
            outcomes[rid] = "ok"
        except (ActivationConflict, ActivationRefused) as exc:
            outcomes[rid] = type(exc).__name__

    threads = [threading.Thread(target=run, args=(r.import_id,)) for r in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes.values()) == ["ActivationConflict", "ok"]
    with app_role_factory.get_session("US") as conn, conn.begin():
        assert conn.execute(
            text("SELECT count(*) FROM business_report_import WHERE period_start=:p AND status='ACTIVE'"), {"p": period[0]}
        ).scalar_one() == 1


def test_db_rejects_two_active_versions_for_same_period(tmp_path, service, app_role_factory):
    """アプリ層を介さない直接UPDATEでも、部分一意インデックスが有効版の同時複数化を拒否する。"""
    period = unique_period()
    a = _stage(service, tmp_path, "a.csv", _csv("B0AAAAAAA1,1,0%,1%,1,0%,$0,1"), period)
    b = _stage(service, tmp_path, "b.csv", _csv("B0BBBBBBB1,1,0%,1%,2,0%,$0,2"), period)
    _activate(service, a.import_id, "first")
    with app_role_factory.get_session("US") as conn:
        with pytest.raises(DBAPIError, match="uk_bri_one_active_per_period"):
            with conn.begin():
                conn.execute(
                    text("UPDATE business_report_import SET status='ACTIVE', activated_at=now() WHERE import_id=:i"),
                    {"i": b.import_id},
                )


def test_overlapping_different_periods_coexist_but_are_never_summed(tmp_path, service, app_role_factory):
    from datetime import date

    base = unique_period()[0].year
    p1 = (date(base, 1, 1), date(base, 1, 31))
    p2 = (date(base, 1, 15), date(base, 2, 15))  # 重なる別期間
    a = _stage(service, tmp_path, "a.csv", _csv("B0SAMEASN1,10,0%,10%,5,0%,$0,5"), p1)
    b = _stage(service, tmp_path, "b.csv", _csv("B0SAMEASN1,10,0%,10%,3,0%,$0,3"), p2)
    _activate(service, a.import_id, "first")
    preview = service.preview_activation(b.import_id, "US", "first")
    assert [o.import_id for o in preview.overlaps] == [a.import_id]  # 警告対象
    service.activate(b.import_id, "US", "first", preview)  # 共存は許可
    with app_role_factory.get_session("US") as conn, conn.begin():
        ca = fetch_candidates(conn, a.import_id)
        cb = fetch_candidates(conn, b.import_id)
        assert [(c.units_sum) for c in ca] == [5] and [(c.units_sum) for c in cb] == [3]  # 合算(8)されない
        # 期間・importを指定しない選択は曖昧なため拒否（自動で選ばない・混ぜない）
        with pytest.raises(CandidateSelectionError, match="複数"):
            select_import(conn, "US")
        assert select_import(conn, "US", period_start=p2[0], period_end=p2[1]).import_id == b.import_id


def test_reject_and_restage_transitions(tmp_path, service, app_role_factory):
    r = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    assert service.reject(r.import_id, "US", "誤ったファイル").status == "REJECTED"
    with pytest.raises(ActivationRefused):
        service.reject(r.import_id, "US", "again")
    preview = service.preview_activation(r.import_id, "US", "first")
    assert preview.blockers  # 不採用は有効化できない
    assert service.restage(r.import_id, "US").status == "STAGED"
    _activate(service, r.import_id, "first")
    with pytest.raises(ActivationRefused):  # 有効版は不採用にできない（差替えで旧版にする）
        service.reject(r.import_id, "US", "x")
    assert [e[0] for e in _events(app_role_factory, r.import_id)] == ["IMPORTED", "REJECTED", "RESTAGED", "ACTIVATED"]


def test_resolve_market_for_import_uses_read_only_role_and_falls_back(tmp_path, service, app_role_factory):
    us = _stage(service, tmp_path, "a.csv", fixture_bytes("synthetic_us.csv"), unique_period())
    jp = _stage(service, tmp_path, "j.csv", fixture_bytes("synthetic_jp.csv"), unique_period(), market="JP")
    assert service.resolve_market_for_import(us.import_id) == "US"
    assert service.resolve_market_for_import(jp.import_id) == "JP"
    assert service.resolve_market_for_import(999999999) is None
    # 参照専用接続なしでも市場別app_roleセッションで解決できる
    fallback = ReportImportService(app_role_factory, None)
    assert fallback.resolve_market_for_import(jp.import_id) == "JP"
    assert fallback.get_report(jp.import_id, "US") is None  # RLS: US セッションからJPのimportは見えない


def test_scan_inbox_reports_history_per_market_not_by_hash_alone(tmp_path, service):
    import uuid

    # モジュール内の他テストと同じfixtureのハッシュが重ならないよう、固有の行を足す
    content = fixture_bytes("synthetic_us.csv") + f"B0{uuid.uuid4().hex[:8].upper()},1,0%,1%,1,0%,$0,1\n".encode()
    period = unique_period()
    res_us, reports = place(tmp_path, "US", "r.csv", content)
    service.import_file(res_us, *period)
    place(tmp_path, "JP", "r.csv", content)  # 同一バイト列がJPフォルダにも存在するが、JPでは未取込
    entries = {e.marketplace: e for e in service.scan_inbox(reports)}
    assert [(h.period_start, h.status) for h in entries["US"].history] == [(period[0], "STAGED")]
    assert entries["JP"].history == []  # ハッシュだけで「取込済み」と判定しない（市場を含めて判定）
    # 別期間の取込があっても、その履歴は期間ごとに別々に並ぶだけ
    period2 = unique_period()
    service.import_file(res_us, *period2)
    entries = {e.marketplace: e for e in service.scan_inbox(reports)}
    assert len(entries["US"].history) == 2
