"""seller_ledger.storefront_blocked.StorefrontBlockedSellerServiceの結合テスト:
実PostgreSQL 16に対してstorefront_blocked_sellerへの記録・重複防止・RLS非適用を検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService

pytestmark = pytest.mark.integration


def test_mark_blocked_records_new_seller(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "PERMANENTLY_EXCLUDED", seller_name="Example Store", note="test note"
        )

    assert result.status == "NEW"
    assert result.info.reason == "PERMANENTLY_EXCLUDED"

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT seller_name, reason, note FROM storefront_blocked_seller WHERE seller_id=:sid"),
            {"sid": seller_id},
        ).one()
        assert row.seller_name == "Example Store"
        assert row.reason == "PERMANENTLY_EXCLUDED"
        assert row.note == "test note"


def test_mark_blocked_does_not_overwrite_existing_record(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        first = StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")
    assert first.status == "NEW"

    with app_role_factory.get_session("US") as conn, conn.begin():
        second = StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "COMPETITOR_SAME_SERVICE", seller_name="Different Name"
        )

    assert second.status == "ALREADY_EXISTS"
    assert second.info.reason == "PERMANENTLY_EXCLUDED"  # 初回の理由が保持される
    assert second.info.first_noted_at == first.info.first_noted_at

    with app_role_factory.get_session("US") as conn:
        row = conn.execute(
            text("SELECT seller_name, reason FROM storefront_blocked_seller WHERE seller_id=:sid"),
            {"sid": seller_id},
        ).one()
        assert row.reason == "PERMANENTLY_EXCLUDED"
        assert row.seller_name is None  # 上書きされていない


def test_mark_blocked_accepts_explicit_historical_first_noted_at(app_role_factory: SessionFactory):
    from datetime import date

    seller_id = f"S{uuid.uuid4().hex[:10]}"
    historical_date = date(2026, 2, 10)

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "PERMANENTLY_EXCLUDED", first_noted_at=historical_date
        )

    assert result.info.first_noted_at == historical_date


def test_get_blocked_info_returns_none_when_not_blocked(app_role_factory: SessionFactory):
    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, f"NOSUCH{uuid.uuid4().hex[:8]}")
    assert info is None


def test_get_blocked_info_returns_info_when_blocked(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE")

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    assert info is not None
    assert info.reason == "COMPETITOR_SAME_SERVICE"


def test_storefront_blocked_seller_is_visible_regardless_of_connection_marketplace(
    app_role_factory: SessionFactory,
):
    """marketplaceに依存しないテーブルのため、US接続で記録した内容がJP接続からも
    そのまま見える（RLS非適用の確認）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

    with app_role_factory.get_session("JP") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    assert info is not None
    assert info.reason == "PERMANENTLY_EXCLUDED"


def test_invalid_reason_is_rejected_by_check_constraint(app_role_factory: SessionFactory):
    from sqlalchemy.exc import IntegrityError

    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with pytest.raises(IntegrityError):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO storefront_blocked_seller (seller_id, reason) "
                    "VALUES (:sid, 'NOT_A_VALID_REASON')"
                ),
                {"sid": seller_id},
            )


def test_mark_blocked_accepts_retry_later_reason(app_role_factory: SessionFactory):
    """RETRY_LATER（同業者利用かは不明だが一時的な理由で今は取得できない、いずれ
    再挑戦したい）が新しい許容値として使えること（ユーザー依頼、docs/DEVIATIONS.md #47
    参照。alembic/versions/0012でCHECK制約に追加）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "RETRY_LATER")

    assert result.status == "NEW"
    assert result.info.reason == "RETRY_LATER"

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is not None
    assert info.reason == "RETRY_LATER"


def test_old_reason_value_high_risk_country_is_no_longer_accepted(app_role_factory: SessionFactory):
    """旧HIGH_RISK_COUNTRYはPERMANENTLY_EXCLUDEDへリネームされたため、CHECK制約に
    もう含まれていないこと（alembic/versions/0011、docs/DEVIATIONS.md #41参照）。"""
    from sqlalchemy.exc import IntegrityError

    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with pytest.raises(IntegrityError):
        with app_role_factory.get_session("US") as conn, conn.begin():
            conn.execute(
                text(
                    "INSERT INTO storefront_blocked_seller (seller_id, reason) "
                    "VALUES (:sid, 'HIGH_RISK_COUNTRY')"
                ),
                {"sid": seller_id},
            )


def test_get_blocked_info_returns_note_when_present(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "PERMANENTLY_EXCLUDED", note="低単価"
        )

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    assert info is not None
    assert info.note == "低単価"


def test_get_blocked_info_returns_none_note_when_not_given(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)

    assert info is not None
    assert info.note is None


def test_delete_blocked_removes_existing_record_and_returns_its_info(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "COMPETITOR_SAME_SERVICE", note="間違えて登録"
        )

    with app_role_factory.get_session("US") as conn, conn.begin():
        deleted = StorefrontBlockedSellerService().delete_blocked(conn, seller_id)

    assert deleted is not None
    assert deleted.reason == "COMPETITOR_SAME_SERVICE"
    assert deleted.note == "間違えて登録"

    with app_role_factory.get_session("US") as conn:
        info = StorefrontBlockedSellerService().get_blocked_info(conn, seller_id)
    assert info is None


def test_delete_blocked_returns_none_when_not_registered(app_role_factory: SessionFactory):
    seller_id = f"NOSUCH{uuid.uuid4().hex[:8]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        deleted = StorefrontBlockedSellerService().delete_blocked(conn, seller_id)
    assert deleted is None


def test_escalate_upgrades_competitor_same_service_to_permanently_excluded(app_role_factory: SessionFactory):
    """mark_or_escalate_to_permanently_excluded(): COMPETITOR_SAME_SERVICEから
    PERMANENTLY_EXCLUDEDへ更新し、first_noted_atは変更しないこと（ユーザー依頼、
    docs/DEVIATIONS.md #54参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        original = StorefrontBlockedSellerService().mark_blocked(
            conn, seller_id, "COMPETITOR_SAME_SERVICE", note="元のメモ"
        )

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_or_escalate_to_permanently_excluded(
            conn, seller_id, new_record_note="自動記録（新規時のメモ）"
        )

    assert result.status == "UPGRADED"
    assert result.info.reason == "PERMANENTLY_EXCLUDED"
    assert result.info.note == "自動判定: 商品0件（元の理由: 同業者利用）。元のメモ"
    assert result.info.first_noted_at == original.info.first_noted_at


def test_escalate_upgrades_retry_later_to_permanently_excluded(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "RETRY_LATER")

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_or_escalate_to_permanently_excluded(
            conn, seller_id, new_record_note="自動記録"
        )

    assert result.status == "UPGRADED"
    assert result.info.reason == "PERMANENTLY_EXCLUDED"
    assert result.info.note == "自動判定: 商品0件（元の理由: 一時的に取得できない）"


def test_escalate_does_not_change_already_permanently_excluded(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED", note="元の理由")

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_or_escalate_to_permanently_excluded(
            conn, seller_id, new_record_note="自動記録"
        )

    assert result.status == "ALREADY_EXCLUDED"
    assert result.info.note == "元の理由"


def test_escalate_records_new_seller_when_not_previously_blocked(app_role_factory: SessionFactory):
    seller_id = f"S{uuid.uuid4().hex[:10]}"

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_or_escalate_to_permanently_excluded(
            conn, seller_id, new_record_note="自動記録（新規）"
        )

    assert result.status == "NEW"
    assert result.info.reason == "PERMANENTLY_EXCLUDED"
    assert result.info.note == "自動記録（新規）"


def test_delete_blocked_then_mark_blocked_allows_re_registration_with_new_reason(
    app_role_factory: SessionFactory,
):
    """誤った理由で登録してしまった場合の訂正フロー: 削除後は正しい理由で再登録できること
    （ユーザー依頼、docs/DEVIATIONS.md #42参照）。"""
    seller_id = f"S{uuid.uuid4().hex[:10]}"
    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "COMPETITOR_SAME_SERVICE")

    with app_role_factory.get_session("US") as conn, conn.begin():
        StorefrontBlockedSellerService().delete_blocked(conn, seller_id)

    with app_role_factory.get_session("US") as conn, conn.begin():
        result = StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

    assert result.status == "NEW"
    assert result.info.reason == "PERMANENTLY_EXCLUDED"
