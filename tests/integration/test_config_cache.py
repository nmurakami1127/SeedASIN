"""ConfigCache結合テスト: 実PostgreSQL 16に対してget()/get_all()のTTLキャッシュ挙動を検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text

from config.config_cache import ConfigCache
from db.session import SessionFactory

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cleanup(app_role_factory: SessionFactory):
    yield
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("DELETE FROM processing_limit_config WHERE marketplace = 'US'"))
        conn.execute(text("DELETE FROM search_condition_config WHERE marketplace = 'US'"))


def test_get_returns_single_row_by_key(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO processing_limit_config (marketplace, limit_type, limit_value) "
                "VALUES ('US', 'ASIN_SEARCH', 500)"
            )
        )

    cache = ConfigCache(app_role_factory, "US")
    row = cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"})
    assert row is not None
    assert row["limit_value"] == 500


def test_get_returns_none_when_no_matching_row(app_role_factory: SessionFactory):
    cache = ConfigCache(app_role_factory, "US")
    row = cache.get("processing_limit_config", {"limit_type": "SEED_OUTPUT"})
    assert row is None


def test_get_caches_within_ttl_and_does_not_see_db_update(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO processing_limit_config (marketplace, limit_type, limit_value) "
                "VALUES ('US', 'ASIN_SEARCH', 100)"
            )
        )

    cache = ConfigCache(app_role_factory, "US")
    first = cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"}, ttl=60)
    assert first["limit_value"] == 100

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "UPDATE processing_limit_config SET limit_value = 999 "
                "WHERE marketplace='US' AND limit_type='ASIN_SEARCH'"
            )
        )

    # TTL内なのでキャッシュされた古い値のまま
    cached = cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"}, ttl=60)
    assert cached["limit_value"] == 100

    cache.invalidate("processing_limit_config")
    fresh = cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"}, ttl=60)
    assert fresh["limit_value"] == 999


def test_get_refetches_after_ttl_expires(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO processing_limit_config (marketplace, limit_type, limit_value) "
                "VALUES ('US', 'ASIN_SEARCH', 1)"
            )
        )

    cache = ConfigCache(app_role_factory, "US")
    cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"}, ttl=0)

    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "UPDATE processing_limit_config SET limit_value = 2 "
                "WHERE marketplace='US' AND limit_type='ASIN_SEARCH'"
            )
        )

    time.sleep(0.01)
    refreshed = cache.get("processing_limit_config", {"limit_type": "ASIN_SEARCH"}, ttl=0)
    assert refreshed["limit_value"] == 2


def test_get_all_lists_multiple_rows_for_marketplace_and_filter(app_role_factory: SessionFactory):
    id1, id2 = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO search_condition_config (config_id, marketplace, source, category) "
                "VALUES (:id, 'US', 'CATEGORY_SEARCH', 'electronics')"
            ),
            {"id": id1},
        )
        conn.execute(
            text(
                "INSERT INTO search_condition_config (config_id, marketplace, source, category) "
                "VALUES (:id, 'US', 'CATEGORY_SEARCH', 'toys')"
            ),
            {"id": id2},
        )

    cache = ConfigCache(app_role_factory, "US")
    rows = cache.get_all("search_condition_config", {"source": "CATEGORY_SEARCH"})
    assert {r["config_id"] for r in rows} == {id1, id2}


def test_get_all_is_scoped_to_marketplace_by_rls(app_role_factory: SessionFactory):
    us_id, jp_id = f"t-{uuid.uuid4()}", f"t-{uuid.uuid4()}"
    try:
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'US', 'CATEGORY_SEARCH')"
                ),
                {"id": us_id},
            )
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO search_condition_config (config_id, marketplace, source) "
                    "VALUES (:id, 'JP', 'CATEGORY_SEARCH')"
                ),
                {"id": jp_id},
            )

        us_cache = ConfigCache(app_role_factory, "US")
        rows = us_cache.get_all("search_condition_config", {"source": "CATEGORY_SEARCH"})
        assert {r["config_id"] for r in rows} == {us_id}
    finally:
        with app_role_factory.get_session("JP") as conn, conn.begin():
            conn.execute(text("DELETE FROM search_condition_config WHERE config_id = :id"), {"id": jp_id})
