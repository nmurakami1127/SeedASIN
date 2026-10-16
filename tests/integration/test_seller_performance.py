"""seller_performance結合テスト: 実PostgreSQL 16に対してAttributionService/
SellerRecommenderService、およびseller_recommendation_configのシードデータを検証する。

共通フィクスチャ（app_role_factory等）はtests/integration/conftest.py参照。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from db.session import SessionFactory
from seller_ledger.seller_service import SellerService
from seller_ledger.storefront_blocked import StorefrontBlockedSellerService
from seller_performance.attribution import AttributionService
from seller_performance.seller_recommender import SellerRecommenderService

pytestmark = pytest.mark.integration


def _seed_seller_and_tool_run(conn, seller_id: str, tool_run_id: str, days_ago: int = 30) -> None:
    """days_agoは「前回の取得」からの経過日数（既定30日、#34のmin_days_since_last規定値
    15日を余裕を持って超える値）。ToolRunLinkService.record()はrequested_atを常にnow()に
    するため指定できず、この用途では生SQLで直接INSERTする。"""
    SellerService().register(conn, "US", seller_id)
    conn.execute(
        text(
            "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
            "VALUES ('US', :sid, :rid, 'SELLER_PAGE', now() - make_interval(days => :days_ago))"
        ),
        {"sid": seller_id, "rid": tool_run_id, "days_ago": days_ago},
    )


def _insert_tool_output(conn, tool_run_id: str, asin: str, acquired_at: str) -> None:
    conn.execute(
        text(
            "INSERT INTO tool_output_asin (marketplace, tool_run_id, asin, acquired_at) "
            "VALUES ('US', :run_id, :asin, :acquired_at)"
        ),
        {"run_id": tool_run_id, "asin": asin, "acquired_at": acquired_at},
    )


def _insert_weekly_listing(conn, asin: str, sku: str, snapshot_date_value: date) -> None:
    conn.execute(
        text(
            "INSERT INTO weekly_listing_snapshot (marketplace, asin, sku, snapshot_date) "
            "VALUES ('US', :asin, :sku, :snapshot_date)"
        ),
        {"asin": asin, "sku": sku, "snapshot_date": snapshot_date_value},
    )


class TestSellerRecommendationConfigSeed:
    def test_defaults_seeded_for_us_and_jp(self, app_role_factory: SessionFactory):
        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text(
                    "SELECT lookback_days, min_listing_count, min_days_since_last "
                    "FROM seller_recommendation_config WHERE marketplace='US'"
                )
            ).one()
            assert row.lookback_days == 90
            assert row.min_listing_count == 2
            assert row.min_days_since_last == 15

        with app_role_factory.get_session("JP") as conn:
            row = conn.execute(
                text(
                    "SELECT lookback_days, min_listing_count, min_days_since_last "
                    "FROM seller_recommendation_config WHERE marketplace='JP'"
                )
            ).one()
            assert row.lookback_days == 90
            assert row.min_listing_count == 2
            assert row.min_days_since_last == 15


class TestAttributionService:
    def test_attributes_asin_to_earliest_tool_run_seller(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin, "USNEW1", date.today())

        service = AttributionService()
        with app_role_factory.get_session("US") as conn:
            count = service.attribute(conn, "US")
        assert count == 1

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text(
                    "SELECT seller_id, first_seen_via_tool_run_id FROM seller_attribution "
                    "WHERE marketplace='US' AND asin=:asin"
                ),
                {"asin": asin},
            ).one()
            assert row.seller_id == seller_id
            assert row.first_seen_via_tool_run_id == tool_run_id

    def test_uses_earliest_tool_output_row_when_asin_seen_via_multiple_sellers(
        self, app_role_factory: SessionFactory
    ):
        early_seller = f"S{uuid.uuid4().hex[:10]}"
        late_seller = f"S{uuid.uuid4().hex[:10]}"
        early_run = f"run-{uuid.uuid4().hex[:8]}"
        late_run = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, early_seller, early_run)
            _seed_seller_and_tool_run(conn, late_seller, late_run)
            _insert_tool_output(conn, late_run, asin, "2026-08-10 10:00:00")
            _insert_tool_output(conn, early_run, asin, "2026-08-01 10:00:00")  # より古い
            _insert_weekly_listing(conn, asin, "USNEW1", date.today())

        service = AttributionService()
        with app_role_factory.get_session("US") as conn:
            service.attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            row = conn.execute(
                text("SELECT seller_id FROM seller_attribution WHERE marketplace='US' AND asin=:asin"),
                {"asin": asin},
            ).one()
            assert row.seller_id == early_seller

    def test_does_not_attribute_asin_without_weekly_listing(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            # weekly_listing_snapshotへは投入しない（未出品）

        service = AttributionService()
        with app_role_factory.get_session("US") as conn:
            count = service.attribute(conn, "US")
        assert count == 0

    def test_is_idempotent_and_does_not_reattribute(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin, "USNEW1", date.today())

        service = AttributionService()
        with app_role_factory.get_session("US") as conn:
            first = service.attribute(conn, "US")
            second = service.attribute(conn, "US")
        assert first == 1
        assert second == 0


class TestSellerRecommenderService:
    def test_recommends_seller_with_listing_occasions(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].listing_occasions == 2

    def test_includes_seller_with_single_listing_occasion(self, app_role_factory: SessionFactory):
        """以前はmin_listing_count（既定2）未満のため除外されていたが、2026-09-30に
        この絞り込みを撤廃したため、1回でも一覧に出ること（ユーザー依頼、
        docs/DEVIATIONS.md #52参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin, "USNEW1", date.today())

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].listing_occasions == 1

    def test_includes_seller_with_zero_listing_occasions(self, app_role_factory: SessionFactory):
        """tool_execution_linkの実績はあるが、weekly_listing_snapshotとの突き合わせが
        1件も成立していない（listing_occasions=0）セラーも一覧に出ること（min_listing_count
        撤廃の核心部分、ユーザー依頼、docs/DEVIATIONS.md #52参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            # weekly_listing_snapshotへは投入しない（出品確認が1件も無い状態）

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].listing_occasions == 0
        assert matching[0].last_requested_date is not None  # tool_execution_link基準は存在する

    def test_excludes_seller_marked_permanently_excluded(self, app_role_factory: SessionFactory):
        """storefront_blocked_sellerにreason='PERMANENTLY_EXCLUDED'として記録済みの
        セラーは、絞り込み条件を満たす実績があっても候補から除外すること
        （Excel移行データ等で過去の実績が残っているケース、ユーザー指摘、
        docs/DEVIATIONS.md #49参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    @pytest.mark.parametrize("reason", ["COMPETITOR_SAME_SERVICE", "RETRY_LATER"])
    def test_does_not_exclude_seller_marked_with_other_reasons(
        self, app_role_factory: SessionFactory, reason: str
    ):
        """COMPETITOR_SAME_SERVICE・RETRY_LATERは「今後も取得する見込みがある」ため、
        PERMANENTLY_EXCLUDEDとは異なり候補から完全除外はしないこと（ユーザー指定、
        docs/DEVIATIONS.md #49参照）。first_noted_atは意図的に古い日付にする——
        #51でこの記録日もmin_days_since_last判定に使われるようになったため、
        「除外しない」という本テストの主旨と「直近すぎる記録は除外する」という#51の
        挙動を混同しないよう分離する（#51固有の挙動はtest_excludes_seller_recently_...
        以下の各テストで別途検証する）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(
                conn, seller_id, reason, first_noted_at=today - timedelta(days=30)
            )

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id in {r.seller_id for r in recs}

    @pytest.mark.parametrize("reason", ["COMPETITOR_SAME_SERVICE", "RETRY_LATER"])
    def test_excludes_seller_recently_marked_competitor_or_retry_later(
        self, app_role_factory: SessionFactory, reason: str
    ):
        """「今回はチェックしたが保留する」（COMPETITOR_SAME_SERVICE・RETRY_LATER）と
        記録した直後のセラーは、tool_execution_linkの実績自体は十分古くても、
        min_days_since_last判定に引っかかり候補から外れること（記録直後に何度も
        同じセラーをチェックしてしまう問題の修正、ユーザー指摘、docs/DEVIATIONS.md
        #51参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=40)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, reason)  # first_noted_at=今日

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    @pytest.mark.parametrize("reason", ["COMPETITOR_SAME_SERVICE", "RETRY_LATER"])
    def test_includes_seller_again_once_enough_days_pass_since_block_record(
        self, app_role_factory: SessionFactory, reason: str
    ):
        """保留記録からmin_days_since_last日以上経過すれば、通常通り再び候補に
        上がってくること（docs/DEVIATIONS.md #51参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=40)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(
                conn, seller_id, reason, first_noted_at=today - timedelta(days=20)
            )

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id in {r.seller_id for r in recs}

    def test_last_requested_date_and_hold_recorded_date_are_shown_as_separate_columns(
        self, app_role_factory: SessionFactory
    ):
        """2026-09-30以降、「最終取得日」（tool_execution_link基準のみ）と「保留記録日」
        （storefront_blocked_seller.first_noted_at）は別々の列として表示すること——
        #51では両者をGREATESTで1列に混ぜていたが、#52でそれぞれ独立した列に分離した
        （ユーザー依頼、docs/DEVIATIONS.md #52参照）。min_days_since_lastの判定・
        並び順自体は引き続き両者のGREATESTを使うため、このセラー自体は候補に残る。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()
        block_date = today - timedelta(days=20)

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=40)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(
                conn, seller_id, "RETRY_LATER", first_noted_at=block_date
            )

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        rec = matching[0]
        assert rec.last_requested_date == today - timedelta(days=40)  # tool_execution_linkのみ基準
        assert rec.hold_recorded_date == block_date
        assert rec.hold_reason == "RETRY_LATER"

    def test_hold_recorded_date_and_reason_are_none_when_not_blocked(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin = f"B{uuid.uuid4().hex[:9].upper()}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin, "USNEW1", date.today())

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].hold_recorded_date is None
        assert matching[0].hold_reason is None

    def test_permanently_excluded_still_excluded_regardless_of_block_record_age(
        self, app_role_factory: SessionFactory
    ):
        """PERMANENTLY_EXCLUDEDは#51の「最終取得日」更新ロジックの対象外——記録日の
        新旧に関わらずNOT EXISTSで完全に除外され続けること（#49の挙動が#51の追加で
        変わっていないことの回帰確認）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=40)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            StorefrontBlockedSellerService().mark_blocked(
                conn, seller_id, "PERMANENTLY_EXCLUDED", first_noted_at=today - timedelta(days=100)
            )

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    def test_seller_with_only_block_record_and_no_tool_execution_link_is_not_recommended(
        self, app_role_factory: SessionFactory
    ):
        """tool_execution_linkの実績が全く無く、storefront_blocked_sellerの記録だけが
        あるセラー（例: まだ一度もASINを取得できていない段階での保留記録）は、
        candidatesの起点であるtool_execution_link自体に行が無いため対象外になること
        （ユーザーからの実装前確認依頼、docs/DEVIATIONS.md #51・#52参照。#52で
        min_listing_countを撤廃しcandidatesの起点をtool_execution_linkへ変更した
        後も、この前提は変わらない）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "RETRY_LATER")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    def test_listing_occasions_excludes_snapshot_outside_lookback_window(
        self, app_role_factory: SessionFactory
    ):
        """min_listing_countの絞り込みは撤廃したが、lookback_daysによる期間制限は
        listing_occasionsの集計そのものに引き続き効いていること——90日超前の実績は
        カウントされない（ユーザー依頼、docs/DEVIATIONS.md #52参照。閾値が無くなった
        ため「除外される」ではなく「カウントされない」ことを検証する形に変更した）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-01-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-01-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=10))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=120))  # 90日超

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].listing_occasions == 1  # 90日超のasin2はカウントされない

    def test_includes_registered_seller_with_no_tool_execution_link(self, app_role_factory: SessionFactory):
        """register済みだが一度もlink-tool-runを実行していないセラーも候補に含め、
        最終取得日・取得回数・合計出品成果等はいずれも「実績なし」を示す値
        （None/0）になること（ユーザー依頼、docs/DEVIATIONS.md #53参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)
            # tool_execution_linkへは一切挿入しない。

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        rec = matching[0]
        assert rec.last_requested_date is None
        assert rec.listing_occasions == 0
        assert rec.total_listed == 0
        assert rec.recent_fetch_days == 0
        assert rec.previous_delta is None
        assert rec.hold_recorded_date is None
        assert rec.hold_reason is None

    def test_excludes_never_linked_seller_if_permanently_excluded(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "PERMANENTLY_EXCLUDED")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    def test_excludes_never_linked_seller_with_recent_hold_record(self, app_role_factory: SessionFactory):
        """tool_execution_linkが無くても、直近のCOMPETITOR_SAME_SERVICE/RETRY_LATER記録が
        あればmin_days_since_lastの判定に引っかかり除外されること（#51の仕組みが
        #53の拡大後も正しく働くことの確認）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)
            StorefrontBlockedSellerService().mark_blocked(conn, seller_id, "RETRY_LATER")  # first_noted_at=今日

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    def test_includes_never_linked_seller_with_old_hold_record(self, app_role_factory: SessionFactory):
        """tool_execution_linkが無く、保留記録が十分古ければ候補に含まれ、
        hold_recorded_date/hold_reasonが表示されること。last_requested_dateは
        引き続きNoneのまま（tool_execution_link基準のみのため）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        today = date.today()
        hold_date = today - timedelta(days=30)
        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", seller_id)
            StorefrontBlockedSellerService().mark_blocked(
                conn, seller_id, "COMPETITOR_SAME_SERVICE", first_noted_at=hold_date
            )

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].last_requested_date is None
        assert matching[0].hold_recorded_date == hold_date
        assert matching[0].hold_reason == "COMPETITOR_SAME_SERVICE"

    def test_never_linked_seller_sorts_before_seller_with_old_requested_date(
        self, app_role_factory: SessionFactory
    ):
        """一度も接点の無いセラー（最終取得日・保留記録日とも無し）は、比較対象の日付が
        無いため並び順の先頭（最優先）に表示されること（ユーザー未指定のため、既存の
        「古いセラーほど上位」という設計思想から妥当と判断、docs/DEVIATIONS.md #53参照）。"""
        never_linked_id = f"S{uuid.uuid4().hex[:10]}"
        old_seller_id = f"S{uuid.uuid4().hex[:10]}"
        old_tool_run_id = f"run-{uuid.uuid4().hex[:8]}"

        with app_role_factory.get_session("US") as conn, conn.begin():
            SellerService().register(conn, "US", never_linked_id)
            _seed_seller_and_tool_run(conn, old_seller_id, old_tool_run_id, days_ago=365)

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        ids_in_order = [r.seller_id for r in recs if r.seller_id in (never_linked_id, old_seller_id)]
        assert ids_in_order == [never_linked_id, old_seller_id]

    def test_excludes_seller_requested_too_recently(self, app_role_factory: SessionFactory):
        """出品成果の閾値は満たしていても、前回の取得（tool_execution_link.requested_at）
        から日が浅すぎるセラーは一覧から除外されること（#34の核心）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=5)  # 直近すぎる
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}

    def test_includes_seller_at_exact_min_days_since_last_boundary(self, app_role_factory: SessionFactory):
        """ちょうどmin_days_since_last日経過している場合は含まれること（N日以上、境界含む）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=15)  # ちょうど境界
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id in {r.seller_id for r in recs}

    def test_reports_total_listed_last_requested_date_and_recent_fetch_days(
        self, app_role_factory: SessionFactory
    ):
        """新規表示項目（合計出品成果・最終取得日・直近90日以内の取得日数）が
        正しく計算されること（ユーザー依頼、docs/DEVIATIONS.md #43参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        run1 = f"run-{uuid.uuid4().hex[:8]}"
        run2 = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin3 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, run1, days_ago=60)
            _seed_seller_and_tool_run(conn, seller_id, run2, days_ago=20)
            _insert_tool_output(conn, run1, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, run2, asin2, "2026-08-20 10:00:00")
            _insert_tool_output(conn, run2, asin3, "2026-08-20 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))
            _insert_weekly_listing(conn, asin3, "USNEW3", today - timedelta(days=5))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        rec = matching[0]
        assert rec.total_listed == 3  # asin1+asin2+asin3、全期間合算
        assert rec.last_requested_date == today - timedelta(days=20)  # run2の取得日
        assert rec.recent_fetch_days == 2  # run1の日・run2の日、いずれも90日以内で別日

    def test_previous_delta_compares_two_most_recent_fetch_days(self, app_role_factory: SessionFactory):
        """前回比は直近の取得日とその1つ前の取得日それぞれの出品成果を比較すること。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        old_run = f"run-{uuid.uuid4().hex[:8]}"
        recent_run = f"run-{uuid.uuid4().hex[:8]}"
        asin_old1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_old2 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_recent = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, old_run, days_ago=60)
            _seed_seller_and_tool_run(conn, seller_id, recent_run, days_ago=20)
            _insert_tool_output(conn, old_run, asin_old1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, old_run, asin_old2, "2026-08-01 10:00:00")
            _insert_tool_output(conn, recent_run, asin_recent, "2026-08-20 10:00:00")
            _insert_weekly_listing(conn, asin_old1, "USNEW1", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin_old2, "USNEW2", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin_recent, "USNEW3", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        # 直近の取得日（recent_run、出品成果1件） - その1つ前（old_run、出品成果2件） = -1
        assert matching[0].previous_delta == -1

    def test_previous_delta_aggregates_same_day_fetches_before_comparing(
        self, app_role_factory: SessionFactory
    ):
        """同じ日に複数回取得している場合は、その日の出品成果を合算してから
        前回比を計算すること（ユーザー確認済み、docs/DEVIATIONS.md #43参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        old_run = f"run-{uuid.uuid4().hex[:8]}"
        recent_run1 = f"run-{uuid.uuid4().hex[:8]}"
        recent_run2 = f"run-{uuid.uuid4().hex[:8]}"
        asin_old1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_old2 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_recent1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_recent2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, old_run, days_ago=60)
            SellerService().register(conn, "US", seller_id)
            # recent_run1/recent_run2は同じ日（20日前）に2回取得したことを模す。
            conn.execute(
                text(
                    "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                    "VALUES ('US', :sid, :rid, 'SELLER_PAGE', now() - make_interval(days => 20))"
                ),
                {"sid": seller_id, "rid": recent_run1},
            )
            conn.execute(
                text(
                    "INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source, requested_at) "
                    "VALUES ('US', :sid, :rid, 'SELLER_PAGE', now() - make_interval(days => 20) + interval '1 hour')"
                ),
                {"sid": seller_id, "rid": recent_run2},
            )
            _insert_tool_output(conn, old_run, asin_old1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, old_run, asin_old2, "2026-08-01 10:00:00")
            _insert_tool_output(conn, recent_run1, asin_recent1, "2026-08-20 10:00:00")
            _insert_tool_output(conn, recent_run2, asin_recent2, "2026-08-20 11:00:00")
            _insert_weekly_listing(conn, asin_old1, "USNEW1", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin_old2, "USNEW2", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin_recent1, "USNEW3", today - timedelta(days=10))
            _insert_weekly_listing(conn, asin_recent2, "USNEW4", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        rec = matching[0]
        # 同じ日の2回（recent_run1+recent_run2）の出品成果を合算(1+1=2)してから
        # その1つ前の日（old_run、2件）と比較する: 2 - 2 = 0
        assert rec.previous_delta == 0
        assert rec.recent_fetch_days == 2  # 同日2回はまとめて1日として数える

    def test_previous_delta_is_none_when_only_one_fetch_day_exists(self, app_role_factory: SessionFactory):
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        tool_run_id = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, tool_run_id, days_ago=30)
            _insert_tool_output(conn, tool_run_id, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, tool_run_id, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].previous_delta is None

    def test_recent_fetch_days_excludes_fetches_older_than_90_days_regardless_of_lookback_days(
        self, app_role_factory: SessionFactory
    ):
        """recent_fetch_daysは--lookback-daysの値に関わらず常に固定90日で数えること
        （ユーザー確認済み、docs/DEVIATIONS.md #43参照）。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        old_run = f"run-{uuid.uuid4().hex[:8]}"  # 120日前 = 90日超なのでrecent_fetch_daysに含めない
        recent_run = f"run-{uuid.uuid4().hex[:8]}"
        asin_old1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_old2 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin_recent = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, old_run, days_ago=120)
            _seed_seller_and_tool_run(conn, seller_id, recent_run, days_ago=20)
            _insert_tool_output(conn, old_run, asin_old1, "2026-04-01 10:00:00")
            _insert_tool_output(conn, old_run, asin_old2, "2026-04-01 10:00:00")
            _insert_tool_output(conn, recent_run, asin_recent, "2026-08-20 10:00:00")
            _insert_weekly_listing(conn, asin_old1, "USNEW1", today - timedelta(days=115))
            _insert_weekly_listing(conn, asin_old2, "USNEW2", today - timedelta(days=115))
            _insert_weekly_listing(conn, asin_recent, "USNEW3", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        # lookback_daysを150に広げて絞り込み条件（120日前の実績）を満たすようにした上で確認する。
        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=150, min_days_since_last=15
            )
        matching = [r for r in recs if r.seller_id == seller_id]
        assert len(matching) == 1
        assert matching[0].recent_fetch_days == 1  # 120日前のrunは90日超のため数えない

    def test_recommendations_are_sorted_by_last_requested_date_then_recent_fetch_days_ascending(
        self, app_role_factory: SessionFactory
    ):
        """並び順: 最終取得日の昇順（古い順）優先、同点ならrecent_fetch_daysの昇順
        （ユーザー指定、docs/DEVIATIONS.md #43参照）。"""
        older_seller = f"S{uuid.uuid4().hex[:10]}"
        newer_seller = f"S{uuid.uuid4().hex[:10]}"
        older_run = f"run-{uuid.uuid4().hex[:8]}"
        newer_run = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin3 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin4 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, older_seller, older_run, days_ago=60)  # より古い最終取得日
            _seed_seller_and_tool_run(conn, newer_seller, newer_run, days_ago=20)  # より新しい最終取得日
            _insert_tool_output(conn, older_run, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, older_run, asin2, "2026-08-01 10:00:00")
            _insert_tool_output(conn, newer_run, asin3, "2026-08-20 10:00:00")
            _insert_tool_output(conn, newer_run, asin4, "2026-08-20 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=50))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=45))
            _insert_weekly_listing(conn, asin3, "USNEW3", today - timedelta(days=15))
            _insert_weekly_listing(conn, asin4, "USNEW4", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        seller_ids_in_order = [r.seller_id for r in recs if r.seller_id in (older_seller, newer_seller)]
        assert seller_ids_in_order == [older_seller, newer_seller]  # 最終取得日が古い方が先

    def test_uses_most_recent_tool_execution_link_when_seller_has_multiple(
        self, app_role_factory: SessionFactory
    ):
        """同じセラーに複数のtool_execution_linkがある場合、最新の1件
        （MAX(requested_at)）で判定すること——古い実行が1件あるだけでは除外を回避できない。"""
        seller_id = f"S{uuid.uuid4().hex[:10]}"
        old_run = f"run-{uuid.uuid4().hex[:8]}"
        recent_run = f"run-{uuid.uuid4().hex[:8]}"
        asin1 = f"B{uuid.uuid4().hex[:9].upper()}"
        asin2 = f"B{uuid.uuid4().hex[:9].upper()}"
        today = date.today()

        with app_role_factory.get_session("US") as conn, conn.begin():
            _seed_seller_and_tool_run(conn, seller_id, old_run, days_ago=60)
            _seed_seller_and_tool_run(conn, seller_id, recent_run, days_ago=5)  # 最新はこちら
            _insert_tool_output(conn, old_run, asin1, "2026-08-01 10:00:00")
            _insert_tool_output(conn, old_run, asin2, "2026-08-01 10:00:00")
            _insert_weekly_listing(conn, asin1, "USNEW1", today - timedelta(days=30))
            _insert_weekly_listing(conn, asin2, "USNEW2", today - timedelta(days=10))

        with app_role_factory.get_session("US") as conn:
            AttributionService().attribute(conn, "US")

        with app_role_factory.get_session("US") as conn:
            recs = SellerRecommenderService().recommend(
                conn, "US", lookback_days=90, min_days_since_last=15
            )
        assert seller_id not in {r.seller_id for r in recs}  # 直近5日前の実行が優先される
