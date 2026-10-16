"""BrandPageCollectorServiceのDB書き込み結合テスト: 実PostgreSQL 16に対して
_start_run/_insert_asins/_finish_run/_update_statusを検証する。

collect()自体（実ブラウザでtarget_urlを開く部分）はPlaywright必須のため対象外
（asin_collector/brand_page_collector.pyのモジュールdocstring参照）。DB書き込みの
挙動はブラウザなしで直接呼び出して検証できるよう分離してある。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from asin_collector.brand_page_collector import BrandPageCollectorService
from db.session import SessionFactory

pytestmark = pytest.mark.integration


def test_start_run_creates_running_collector_run(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT run_type, target, status FROM collector_run WHERE id=:id"), {"id": run_id}
        ).one()
        assert row.run_type == "BRAND_PAGE"
        assert row.target == "https://www.amazon.com/example-brand"
        assert row.status == "RUNNING"


def test_update_status_transitions_to_captcha_wait_and_back(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    service._update_status(app_role_factory, "US", run_id, "CAPTCHA_WAIT")
    with app_role_factory.get_session("US") as conn:
        status = conn.execute(text("SELECT status FROM collector_run WHERE id=:id"), {"id": run_id}).scalar_one()
        assert status == "CAPTCHA_WAIT"

    service._update_status(app_role_factory, "US", run_id, "RUNNING")
    with app_role_factory.get_session("US") as conn:
        status = conn.execute(text("SELECT status FROM collector_run WHERE id=:id"), {"id": run_id}).scalar_one()
        assert status == "RUNNING"


def test_insert_asins_then_finish_run_records_asins_and_completes(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    service._insert_asins(app_role_factory, "US", run_id, ["B0TESTASIN1", "B0TESTASIN2"])
    service._finish_run(app_role_factory, "US", run_id, 2)

    with app_role_factory.get_session("US") as conn:
        run = conn.execute(
            text("SELECT status, asin_count, completed_at FROM collector_run WHERE id=:id"), {"id": run_id}
        ).one()
        assert run.status == "COMPLETED"
        assert run.asin_count == 2
        assert run.completed_at is not None

        asins = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id=:id ORDER BY asin"), {"id": run_id}
        ).fetchall()
        assert [a[0] for a in asins] == ["B0TESTASIN1", "B0TESTASIN2"]


def test_insert_asins_across_multiple_pages_accumulates(app_role_factory: SessionFactory):
    """複数ページ巡回でページ単位に_insert_asins()を呼んだ場合、途中で処理が止まっても
    それまでのページ分がcollector_asinに残っていること（逐次INSERT設計の検証）。"""
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    service._insert_asins(app_role_factory, "US", run_id, ["B0PAGE1ASIN1", "B0PAGE1ASIN2"])
    with app_role_factory.get_session("US") as conn:
        mid_run_asins = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id=:id ORDER BY asin"), {"id": run_id}
        ).fetchall()
        assert [a[0] for a in mid_run_asins] == ["B0PAGE1ASIN1", "B0PAGE1ASIN2"]

    service._insert_asins(app_role_factory, "US", run_id, ["B0PAGE2ASIN1"])
    service._finish_run(app_role_factory, "US", run_id, 3)

    with app_role_factory.get_session("US") as conn:
        all_asins = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id=:id ORDER BY asin"), {"id": run_id}
        ).fetchall()
        assert [a[0] for a in all_asins] == ["B0PAGE1ASIN1", "B0PAGE1ASIN2", "B0PAGE2ASIN1"]


def test_insert_asins_with_empty_list_is_a_no_op(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    service._insert_asins(app_role_factory, "US", run_id, [])

    with app_role_factory.get_session("US") as conn:
        asins = conn.execute(
            text("SELECT asin FROM collector_asin WHERE collector_run_id=:id"), {"id": run_id}
        ).fetchall()
        assert asins == []


def test_finish_run_with_no_asins_still_completes(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/example-brand")

    service._finish_run(app_role_factory, "US", run_id, 0)

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT status, asin_count FROM collector_run WHERE id=:id"), {"id": run_id}
        ).one()
        assert row.status == "COMPLETED"
        assert row.asin_count == 0


def test_cross_marketplace_isolation(app_role_factory: SessionFactory):
    service = BrandPageCollectorService()
    us_run_id = service._start_run(app_role_factory, "US", "https://www.amazon.com/us-brand")
    jp_run_id = service._start_run(app_role_factory, "JP", "https://www.amazon.co.jp/jp-brand")

    with app_role_factory.get_session("US") as conn:
        rows = conn.execute(
            text("SELECT id FROM collector_run WHERE id IN (:a, :b)"), {"a": us_run_id, "b": jp_run_id}
        ).fetchall()
        assert [r[0] for r in rows] == [us_run_id]
