"""seller_performance.seller_recommenderの純粋なロジック（format_recommendation_line）の
単体テスト。DBアクセスを伴うrecommend()自体の検証はtests/integration/test_seller_performance.py
参照。

保留記録日・保留理由の分離（2026-09-30、docs/DEVIATIONS.md #52参照）に伴い、
_rec()ヘルパーはhold_recorded_date/hold_reasonを省略可能な引数として追加した
（省略時はNone＝保留記録なし）。
"""
from datetime import date
from decimal import Decimal

from seller_performance.seller_recommender import SellerRecommendation, format_recommendation_line


def _rec(
    previous_delta,
    total_listed: int = 10,
    listing_occasions: int = 5,
    last_requested_date: date | None = date(2026, 6, 1),
    hold_recorded_date: date | None = None,
    hold_reason: str | None = None,
) -> SellerRecommendation:
    return SellerRecommendation(
        seller_id="SELLER1",
        listing_occasions=listing_occasions,
        last_requested_date=last_requested_date,
        hold_recorded_date=hold_recorded_date,
        hold_reason=hold_reason,
        total_listed=total_listed,
        recent_fetch_days=3,
        previous_delta=previous_delta,
    )


def test_format_recommendation_line_shows_all_fields():
    line = format_recommendation_line(_rec(previous_delta=8))
    assert "seller_id=SELLER1" in line
    assert "最終取得日=2026-06-01" in line
    assert "保留記録日=-" in line
    assert "保留理由=-" in line
    assert "取得日数(重複排除)=3" in line
    assert "取得回数=5" in line
    assert "合計出品成果=10件" in line
    assert "前回比=+8件" in line


def test_format_recommendation_line_shows_negative_delta_with_sign():
    line = format_recommendation_line(_rec(previous_delta=-2))
    assert "前回比=-2件" in line


def test_format_recommendation_line_shows_dash_when_no_previous_fetch_to_compare():
    line = format_recommendation_line(_rec(previous_delta=None))
    assert "前回比=-" in line


def test_format_recommendation_line_handles_decimal_delta():
    """PostgreSQLのSUM(bigint)はnumeric型になり、psycopgはDecimalとしてPythonへ返す
    （実データでの動作確認で発見した実際のバグ、docs/DEVIATIONS.md #43参照。
    seller_recommender.py側でbigintへキャストして防いでいるが、万一Decimalが
    紛れ込んでも表示関数側で壊れないことも合わせて確認する）。"""
    line = format_recommendation_line(_rec(previous_delta=Decimal("8")))
    assert "前回比=+8件" in line


def test_format_recommendation_line_shows_zero_listing_occasions():
    """min_listing_countによる絞り込みを撤廃したため、取得回数0のセラーも
    表示できること（ユーザー依頼、docs/DEVIATIONS.md #52参照）。"""
    line = format_recommendation_line(_rec(previous_delta=None, listing_occasions=0))
    assert "取得回数=0" in line


def test_format_recommendation_line_shows_hold_recorded_date_and_reason():
    """storefront_blocked_sellerにCOMPETITOR_SAME_SERVICE/RETRY_LATERで記録済みの
    場合、保留記録日・保留理由（REASON_LABELS由来の表示ラベル）を表示すること
    （ユーザー依頼、docs/DEVIATIONS.md #52参照）。"""
    line = format_recommendation_line(
        _rec(
            previous_delta=None,
            hold_recorded_date=date(2026, 9, 17),
            hold_reason="RETRY_LATER",
        )
    )
    assert "保留記録日=2026-09-17" in line
    assert "保留理由=一時的に取得できない" in line


def test_format_recommendation_line_shows_hold_reason_label_for_competitor_same_service():
    line = format_recommendation_line(
        _rec(previous_delta=None, hold_recorded_date=date(2026, 9, 1), hold_reason="COMPETITOR_SAME_SERVICE")
    )
    assert "保留理由=同業者利用" in line


def test_format_recommendation_line_shows_dash_when_last_requested_date_is_none():
    """last_requested_dateは通常candidatesの起点（tool_execution_link）に必ず
    存在するため実質的には発生しないが、防御的にNoneでも安全に表示できることを
    確認する（ユーザー依頼、docs/DEVIATIONS.md #52参照）。"""
    line = format_recommendation_line(_rec(previous_delta=None, last_requested_date=None))
    assert "最終取得日=-" in line
