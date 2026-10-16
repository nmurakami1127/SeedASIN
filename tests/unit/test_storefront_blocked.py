"""seller_ledger.storefront_blockedの純粋なロジック（reason_label）の単体テスト。

DBアクセスを伴うmark_blocked/get_blocked_infoの検証はtests/integration/
test_storefront_blocked.py参照。
"""
from datetime import date

from seller_ledger.storefront_blocked import BlockedSellerInfo


def test_reason_label_for_permanently_excluded():
    info = BlockedSellerInfo(seller_id="S1", reason="PERMANENTLY_EXCLUDED", first_noted_at=date(2026, 9, 9))
    assert info.reason_label == "今後も取得しない"


def test_reason_label_for_competitor_same_service():
    info = BlockedSellerInfo(seller_id="S1", reason="COMPETITOR_SAME_SERVICE", first_noted_at=date(2026, 9, 9))
    assert info.reason_label == "同業者利用"


def test_reason_label_for_retry_later():
    """同業者利用かは不明だが一時的な理由で今は取得できず、いずれ再挑戦したいケース
    （ユーザー依頼、docs/DEVIATIONS.md #47参照）。"""
    info = BlockedSellerInfo(seller_id="S1", reason="RETRY_LATER", first_noted_at=date(2026, 9, 9))
    assert info.reason_label == "一時的に取得できない"


def test_reason_label_falls_back_to_raw_value_for_unknown_reason():
    info = BlockedSellerInfo(seller_id="S1", reason="SOMETHING_ELSE", first_noted_at=date(2026, 9, 9))
    assert info.reason_label == "SOMETHING_ELSE"
