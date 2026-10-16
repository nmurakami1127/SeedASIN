"""CheckRecommendationsServiceのDB書き込み結合テスト: 実PostgreSQL 16に対して
_record_no_products()を検証する。

check_sellers()自体（実ブラウザでストアフロントページを開く部分）はPlaywright必須の
ため対象外（asin_collector/check_recommendations.pyのモジュールdocstring、
asin_collector/brand_page_collector.pyの既存パターンを踏襲、docs/DEVIATIONS.md #53
参照）。DB書き込みの挙動はブラウザなしで直接呼び出して検証できるよう分離してある。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest

from asin_collector.check_recommendations import CheckRecommendationsService
from db.session import SessionFactory
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

pytestmark = pytest.mark.integration


def test_record_no_products_marks_seller_permanently_excluded(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    outcome = CheckRecommendationsService()._record_no_products(app_role_factory, "US", seller_id)

    assert outcome == "NEW"
    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.note == "check-recommendationsでストアフロント検索結果0件を確認（自動記録）"


def test_record_no_products_upgrades_competitor_same_service_hold(app_role_factory: SessionFactory):
    """COMPETITOR_SAME_SERVICEで記録済みのセラーが商品0件と確認できた場合、
    PERMANENTLY_EXCLUDEDへ更新すること。first_noted_atは変更せず、noteの先頭に
    自動判定の経緯を追記し元のnoteを後ろに残すこと（ユーザー依頼、
    docs/DEVIATIONS.md #54参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE", note="先に記録済み")
    with app_role_factory.get_session("US") as conn:
        original = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    outcome = CheckRecommendationsService()._record_no_products(app_role_factory, "US", seller_id)

    assert outcome == "UPGRADED"
    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.note == "自動判定: 商品0件（元の理由: 同業者利用）。先に記録済み"
    assert info.first_noted_at == original.first_noted_at


def test_record_no_products_upgrades_retry_later_hold_without_original_note(app_role_factory: SessionFactory):
    """RETRY_LATERで記録済みのセラーも同様にPERMANENTLY_EXCLUDEDへ更新すること。
    元のnoteが無い場合は自動判定のnoteのみになること。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "RETRY_LATER")

    outcome = CheckRecommendationsService()._record_no_products(app_role_factory, "US", seller_id)

    assert outcome == "UPGRADED"
    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.note == "自動判定: 商品0件（元の理由: 一時的に取得できない）"


def test_record_no_products_does_not_change_already_permanently_excluded(app_role_factory: SessionFactory):
    """既にPERMANENTLY_EXCLUDEDのセラーは何もしない（ユーザー指定）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note="元の理由")

    outcome = CheckRecommendationsService()._record_no_products(app_role_factory, "US", seller_id)

    assert outcome == "ALREADY_EXCLUDED"
    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info.reason == "PERMANENTLY_EXCLUDED"
    assert info.note == "元の理由"
