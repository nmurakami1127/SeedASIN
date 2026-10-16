"""各cli.pyのargparse構成の単体テスト（DB不要）。実際のDB書き込みを伴うエンドツーエンドの
配線検証はtests/integration/test_cli.pyを参照。"""
import pytest

import asin_collector.cli as asin_collector_cli
import seller_ledger.cli as seller_ledger_cli
import seller_performance.cli as seller_performance_cli
import tool_intake.cli as tool_intake_cli


class TestSellerLedgerCli:
    def test_register_requires_marketplace_seller_id(self):
        """registerに--sourceは無い（discovery_source廃止、docs/DEVIATIONS.md #25）。"""
        parser = seller_ledger_cli.build_parser()
        args = parser.parse_args(["register", "--marketplace=US", "--seller-id=S1"])
        assert args.command == "register"
        assert args.marketplace == "US"
        assert args.seller_id == "S1"
        assert args.source_asin is None
        assert not hasattr(args, "source")

    def test_register_missing_required_arg_exits(self):
        parser = seller_ledger_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["register", "--marketplace=US"])

    def test_register_invalid_marketplace_exits(self):
        parser = seller_ledger_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["register", "--marketplace=UK", "--seller-id=S1"])

    def test_register_source_asin_optional(self):
        parser = seller_ledger_cli.build_parser()
        args = parser.parse_args(
            ["register", "--marketplace=US", "--seller-id=S1", "--source-asin=B0TESTASN1"]
        )
        assert args.source_asin == "B0TESTASN1"

    def test_link_tool_run_parses(self):
        """--sourceは廃止済み（どのロジックからも参照されない自由入力で表記ゆれの原因
        だったため、ユーザー依頼、docs/DEVIATIONS.md #48参照）。"""
        parser = seller_ledger_cli.build_parser()
        args = parser.parse_args(
            ["link-tool-run", "--marketplace=JP", "--seller-id=S1", "--tool-run-id=7128337"]
        )
        assert args.command == "link-tool-run"
        assert args.tool_run_id == "7128337"
        assert not hasattr(args, "source")


class TestToolIntakeCli:
    def test_import_tool_output_parses(self):
        parser = tool_intake_cli.build_parser()
        args = parser.parse_args(
            ["import-tool-output", "--marketplace=US", "--tool-run-id=7128337", "--file=out.csv"]
        )
        assert args.command == "import-tool-output"
        assert args.file == "out.csv"

    def test_import_tool_output_file_is_optional(self):
        """--fileを省略した場合はNone（自動検出に回す、docs/DEVIATIONS.md #37参照）。"""
        parser = tool_intake_cli.build_parser()
        args = parser.parse_args(["import-tool-output", "--marketplace=US", "--tool-run-id=7128337"])
        assert args.file is None

    def test_import_weekly_listing_parses(self):
        """snapshot_dateはCSV内の登録日時列から行ごとに決まるため、--snapshot-dateは
        存在しない（2026-09-09の仕様変更、docs/DEVIATIONS.md #33参照）。"""
        parser = tool_intake_cli.build_parser()
        args = parser.parse_args(["import-weekly-listing", "--marketplace=US", "--file=listing.csv"])
        assert args.command == "import-weekly-listing"
        assert args.file == "listing.csv"
        assert not hasattr(args, "snapshot_date")

    def test_sync_pending_parses(self):
        parser = tool_intake_cli.build_parser()
        args = parser.parse_args(["sync-pending", "--marketplace=US"])
        assert args.command == "sync-pending"
        assert args.marketplace == "US"

    def test_sync_pending_requires_marketplace(self):
        parser = tool_intake_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["sync-pending"])


class TestSellerPerformanceCli:
    def test_attribute_parses(self):
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(["attribute", "--marketplace=US"])
        assert args.command == "attribute"

    def test_recommend_thresholds_default_to_none(self):
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(["recommend", "--marketplace=US"])
        assert args.lookback_days is None
        assert args.min_days_since_last is None

    def test_recommend_thresholds_override(self):
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(
            [
                "recommend",
                "--marketplace=US",
                "--lookback-days=60",
                "--min-days-since-last=30",
            ]
        )
        assert args.lookback_days == 60
        assert args.min_days_since_last == 30

    def test_export_all_output_dir_defaults_to_none(self):
        """省略時はseller_performance.cli内の既定パス（exports/analysis/<timestamp>/）を
        CLI側で組み立てる（ユーザー依頼、docs/DEVIATIONS.md #55参照）。"""
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(["export-all"])
        assert args.command == "export-all"
        assert args.output_dir is None

    def test_export_all_output_dir_override(self):
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(["export-all", "--output-dir=C:\\tmp\\analysis"])
        assert args.output_dir == "C:\\tmp\\analysis"

    def test_recommend_has_no_min_listing_count_argument(self):
        """min_listing_countによる絞り込みは撤廃済み（ユーザー依頼、
        docs/DEVIATIONS.md #52参照）。--min-listing-count自体が存在しないこと。"""
        parser = seller_performance_cli.build_parser()
        args = parser.parse_args(["recommend", "--marketplace=US"])
        assert not hasattr(args, "min_listing_count")

        with pytest.raises(SystemExit):
            parser.parse_args(["recommend", "--marketplace=US", "--min-listing-count=3"])


class TestAsinCollectorCli:
    def test_collect_brand_page_parses(self):
        """--marketplaceは廃止済み。marketplaceはtarget-urlのドメインから自動判定する
        （docs/DEVIATIONS.md #21）。"""
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(
            ["collect-brand-page", "--target-url=https://www.amazon.com/s?k=porter+bag"]
        )
        assert args.command == "collect-brand-page"
        assert args.target_url == "https://www.amazon.com/s?k=porter+bag"
        assert not hasattr(args, "marketplace")

    def test_collect_brand_page_missing_target_url_exits(self):
        parser = asin_collector_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["collect-brand-page"])

    def test_export_run_collector_run_id_optional(self):
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(["export-run"])
        assert args.command == "export-run"
        assert args.collector_run_id is None

    def test_export_run_collector_run_id_parses_as_int(self):
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(["export-run", "--collector-run-id=3"])
        assert args.collector_run_id == 3

    def test_check_recommendations_parses(self):
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(
            ["check-recommendations", "--marketplace=JP", "--csv=exports/recommend/recommend_JP_20261007.csv"]
        )
        assert args.command == "check-recommendations"
        assert args.marketplace == "JP"
        assert args.csv == "exports/recommend/recommend_JP_20261007.csv"

    def test_check_recommendations_requires_marketplace_and_csv(self):
        parser = asin_collector_cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["check-recommendations", "--csv=recommend.csv"])
        with pytest.raises(SystemExit):
            parser.parse_args(["check-recommendations", "--marketplace=JP"])

    def test_check_recommendations_max_consecutive_undetermined_defaults_to_5(self):
        """判定不能の連続中断閾値の既定値は5（ユーザー依頼、docs/DEVIATIONS.md #54参照）。"""
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(
            ["check-recommendations", "--marketplace=JP", "--csv=recommend.csv"]
        )
        assert args.max_consecutive_undetermined == 5

    def test_check_recommendations_max_consecutive_undetermined_override(self):
        parser = asin_collector_cli.build_parser()
        args = parser.parse_args(
            [
                "check-recommendations",
                "--marketplace=JP",
                "--csv=recommend.csv",
                "--max-consecutive-undetermined=10",
            ]
        )
        assert args.max_consecutive_undetermined == 10


class TestMarketplaceFromUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.amazon.co.jp/s?k=porter",
            "https://amazon.co.jp/s?k=porter",
            "http://www.amazon.co.jp/dp/B0TESTASN1",
        ],
    )
    def test_detects_jp(self, url):
        assert asin_collector_cli.marketplace_from_url(url) == "JP"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.amazon.com/s?k=porter+bag",
            "https://amazon.com/s?k=porter+bag",
            "http://www.amazon.com/dp/B0TESTASN1",
        ],
    )
    def test_detects_us(self, url):
        assert asin_collector_cli.marketplace_from_url(url) == "US"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.amazon.co.uk/s?k=porter",
            "https://www.rakuten.co.jp/",
            "not-a-url",
            "",
        ],
    )
    def test_unsupported_domain_raises(self, url):
        with pytest.raises(SystemExit):
            asin_collector_cli.marketplace_from_url(url)
