"""workflowの設定（JSON）。試行用の小さい初期値を既定とし、安全な範囲（ハード上限）の中でだけ変更できる。

実行のたびに入力させない: ACTIVEレポートのimport_id・市場・各上限は設定ファイルに書く。内部ID（投入キー・ログJSON・options JSON・
セレクタ）は設定にもコマンドにも出てこない（実画面で確認済みの既定値を使い、画面の設定値は読み取るだけ）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from acquisition.partition import (
    DEFAULT_INITIAL_EDGES_YEN, DEFAULT_MAX_DEPTH, DEFAULT_MIN_WIDTH_YEN, PartitionConfig, PartitionError, validate_markets,
)

MARKETS = ("US", "JP")
OPPOSITE = {"US": "JP", "JP": "US"}
ROUTES = ("SELLER", "BRAND")
SELECTION_MODES = ("ordered_only", "ordered_then_zero", "zero_only")

# 安全な範囲（これを超える設定は拒否する）。出典発見側の上限（acquisition.discovery.HARD_CAPS）と同じか、より厳しい。
HARD = {
    "origin_asins": 10, "sources_per_origin": 10, "pages_per_source": 200, "cross_asins": 5, "cross_sources_per_asin": 5,
    "max_total_pages": 200, "max_new_sources": 100, "max_submit_asins": 50000, "tool_max_asins": 5000,
}


class ConfigError(Exception):
    pass


@dataclass
class Lane:
    """起点レポートの市場ごとの設定。origin_import は**市場ごとに明示したACTIVEレポート**（過去のimportを自動では混ぜない）。"""
    origin_import: int
    page_market: str | None = None  # 商品ページ（発見）と出典の取得先の市場。省略時は起点レポートの市場と同じ
    routes: list[str] = field(default_factory=lambda: ["SELLER"])
    selection_mode: str = "ordered_then_zero"  # ordered_only=注文ありのみ / ordered_then_zero=注文あり→注文0点 / zero_only=注文0点のみ
    # 既存の出典の取得候補にだけ加える、反対側市場のACTIVEレポートのimport_id（既定は空=従来と同じ）。起点ASINの発見（discover）・cross には使わない。
    # 例: USレーン(origin_import=1)に [2]（JP市場のレポート）を書くと、レポート2由来の経路しか持たないUS市場の出典が取得候補に入る。docs/DEVIATIONS.md #100
    extra_origin_imports: list[int] = field(default_factory=list)


@dataclass
class Limits:
    origin_asins: int = 1          # 起点ASIN（市場ごと）
    sources_per_origin: int = 3    # 起点ごとの発見出典
    pages_per_source: int = 2      # 1出典の取得ページ
    max_asins_per_source: int | None = None
    cross_asins: int = 1           # 反対側市場への展開（取得結果から、市場ごと）
    cross_sources_per_asin: int = 3


@dataclass
class Totals:
    """実行全体の上限（工程別の上限とは別）。"""
    max_total_pages: int = 24      # 出典の取得ページの合計
    max_new_sources: int = 12      # 新規に登録する出典の合計
    max_submit_asins: int = 5000   # 統合ツールへ投入するASINの合計
    max_new_submissions_per_direction: int | None = 1  # この実行で新しく投入する件数（方向ごと）。null=無制限。引き継ぎ済みの投入は数えない


@dataclass
class Daily:
    """watch全体で共有する日次上限（Asia/Tokyoの午前0時で区切る）。nullは無制限。再起動・新しいworkflow・resumeでリセットされない。
    既定値は**試運転用**で、通常運用の確定値ではない（config/workflow.standard.example.json と README の提案を参照）。"""
    max_pages: int | None = 24                          # Amazon新規取得ページ（出典の取得 + 商品ページ調査。保存済み結果の再利用は含まない）
    max_new_submissions_per_direction: int | None = 2   # 統合ツールへの新規投入（方向ごと）。SAVE後の結果不明も使用済み
    max_submit_asins: int | None = 5000                 # 投入ASIN数の合計


@dataclass
class Watch:
    interval_minutes: int = 30          # 対象が無い時の再確認間隔
    failure_retry_minutes: int = 30     # 失敗した工程を再試行するまでの冷却（連続再試行しない）
    human_wait_retry_minutes: int = 30  # 人手待ち（ログイン・CAPTCHA等）を再確認する間隔
    notify_minutes: int = 360           # 同じ人手待ちの表示を繰り返す最短間隔


@dataclass
class Stall:
    """止まりの検知（watch_status.jsonとターミナルの警告。workflow/stall.py、docs/DEVIATIONS.md #79）。表示が増えるだけで、探索・登録の動作は変えない。"""
    server_error_streak: int = 5       # 商品ページの取得がSERVER_ERRORで何回連続したら警告にするか
    new_asin_min_percent: int = 30     # 直近24時間の初見ASINが、直近7日の日次の中央値のこの%未満なら警告


@dataclass
class Cross:
    enabled: bool = True
    fetch_discovered: bool = False  # 反対側で新たに発見した出典の自動取得（初期OFF。再帰的な探索はしない）


@dataclass
class PriceBand:
    """価格帯分割（docs/DEVIATIONS.md #83）。markets に書いた**取得先市場**のSELLER出典の一巡だけを、価格帯ごとに走査する。既定は空（無効）。
    使えるのはJPのみ（USは検証で拒否）。close_on_repeated_page=true が前提。"""
    markets: list[str] = field(default_factory=list)
    initial_edges_yen: list[int] = field(default_factory=lambda: list(DEFAULT_INITIAL_EDGES_YEN))
    min_width_yen: int = DEFAULT_MIN_WIDTH_YEN
    max_depth: int = DEFAULT_MAX_DEPTH
    treat_end_as_depth_limit_pages: int | None = None  # 今は使わない（null）。整数Nなら、「次へ」が消えたN ページ以上の帯を深さの限界として扱う


@dataclass
class Partition:
    price_band: PriceBand = field(default_factory=PriceBand)


@dataclass
class Completion:
    """出典の効率による優先度の調整と取り直しの見送り（docs/DEVIATIONS.md #98）。しきい値はここに集約する。
    **除外ではない**: 優先度を下げるだけで、再評価日が来る前でも候補が足りなければ戻す。ページ上限(totals)・日次上限・CAPTCHA対応は緩めない。"""
    low_yield_window_runs: int = 3          # 判定に使う直近の評価run数（初見率はこの窓の合算で見る）
    low_yield_window_days: int = 3          # 窓に入れる期間（日）
    low_yield_min_pages: int = 10           # 1回の取得がこのページ数以上のときだけ標本にする
    low_yield_window_min_pages: int = 20    # 窓の合計がこのページ数以上のときだけ判定する
    low_yield_new_rate: float = 0.10        # 窓の初見率がこの割合未満なら低効率
    backoff_start_days: int = 1             # 後退の期間の最初（日）。低効率が連続するたびに2倍
    backoff_max_days: int = 14              # 後退の最長（日）
    reserve_unevaluated: int = 3            # 1方向あたり、未評価（標本の無い）出典に確保する枠
    min_selectable: int = 0                 # 選択可能な出典がこの数を下回ると、後退中の出典を戻す。0=自動(ceil(max_total_pages/pages_per_source))
    restart_release_when_scarce: bool = True  # 取り直し見送り中でも、他に取得できる候補が足りなければ見送りを解除する


@dataclass
class AutoRegistration:
    """統合ツールへの登録の自動復旧（docs/DEVIATIONS.md #102）。既定は**無効**（従来どおり）。有効にしても、SAVEは1回しか押さず、
    結果不明（SAVE後）の投入は再送しない。再送してよいのは「SAVE前の失敗を確認できた」場合だけ。"""
    enabled: bool = False
    # SAVE直後は、5,000件の保存でlogs.phpが重くなり読めないことがある。最初の読み取りの前に待つ秒数
    post_save_wait_seconds: int = 15
    # ログ画面を読めない（LogUnreadable）ときの再試行の待ち（秒。段階的に延ばす。読み取りのたびにページを読み込み直す）。既定の合計は5分
    log_unreadable_waits_seconds: list[int] = field(default_factory=lambda: [30, 60, 90, 120])
    # 結果不明(UNKNOWN)が自動照合で解決しないまま、この分数を超え、ログを読めた確認が2回以上ある場合、人の判断待ち(needs-human)に記録する
    needs_human_after_minutes: int = 180
    # true=needs-humanに記録した結果不明の投入があっても、他の独立した投入の登録を続ける。false(既定)=従来どおり、解決するまで次の投入に進まない
    continue_past_needs_human: bool = False
    # SAVE前の一時的な失敗（goto/fillのタイムアウト）で試行上限に達した投入へ、待ちを置いて自動で再試行権を付与する
    pre_save_retry_wait_minutes: int = 30
    pre_save_retry_max_grants: int = 2     # 1投入あたりの自動付与の上限回数。0=自動付与しない
    loop_interval_minutes: int = 10        # 常駐ループ(workflow/registration_loop.py)の間隔
    # 登録ループ（--only-keys含む）で統合ツールが未ログインのとき、同じ周期内でログイン完了を待つ秒数（#104）。0以下=従来の3秒相当（待たない）。-3600〜3600。
    # 探索watch（keep_tool_open）の3秒は変えない（この設定は登録ループだけが使う）。
    login_wait_seconds: int = 600


@dataclass
class WorkflowConfig:
    lanes: dict[str, Lane]
    limits: Limits = field(default_factory=Limits)
    totals: Totals = field(default_factory=Totals)
    cross: Cross = field(default_factory=Cross)
    stall: Stall = field(default_factory=Stall)
    brand_enabled: bool = False     # 未検証のブランド取得（初期OFF。SELLERから開始）
    tool_max_asins: int = 5000      # 統合ツールの1回の入力（画面の案内は5,000件まで）
    result_max_age_days: int = 30   # 保存済みの取得結果を投入に使ってよい鮮度
    state_dir: str = "exports/workflow"
    submission_dir: str = "exports/tool_submission"
    page_revisit_days: int = 30     # 同じ商品ページの再調査間隔（出典の取得間隔 = acquisition側の設定 とは別）
    daily: Daily = field(default_factory=Daily)
    watch: Watch = field(default_factory=Watch)
    profile: str = "trial"          # 表示用の名前（trial=試運転 / standard=通常運用）。動作は上の値だけで決まる
    listing_feedback: bool = False  # 出品確定実績のあるセラーを辿れる起点を優先。候補の足切りはしない
    skip_failures: bool = False  # 利用者の指示で失敗をスキップ。結果不明は履歴・占有を残して追跡取り下げ
    batch_target_asins: int = 0  # 0=従来方式。続き取得を同一出典・方向でこの件数まで蓄積し、一巡完了時に端数を送る
    submission_enabled: bool = True  # False=探索と投入計画の保存のみ。登録は独立した手動実行
    tool_session_monitor: bool = False  # ログインの定期監視・維持アクセスは明示的に有効化した場合だけ
    close_on_repeated_page: bool = False  # true=保存済みのページの直後に同じ内容が返ったら、1回取り直して確認し、同じなら検索結果の深さの限界として
    #                                      SUCCESS_TRUNCATED(DEPTH_LIMIT)・再開位置なしで閉じる。false=従来どおり（REPEATED_PAGEで一巡をBROKENにする）。docs/DEVIATIONS.md #80
    partition: Partition = field(default_factory=Partition)  # 価格帯分割（既定は無効）。docs/DEVIATIONS.md #83
    completion: Completion = field(default_factory=Completion)  # 出典の効率による優先度・取り直し見送り。docs/DEVIATIONS.md #98
    # true=batch_target_asins未満の端数も、同じ一巡の続きを待つ時間(flush_min_age_minutes)が過ぎたら投入計画に回す（5,000件待ちの廃止）。
    # false(既定)=従来どおり、末尾到達（COMPLETE/DEPTH_LIMIT）までは端数を持ち越す。batch_target_asins>0が前提。docs/DEVIATIONS.md #102
    flush_unfilled_batches: bool = False
    flush_min_age_minutes: int = 30  # 同じ一巡の最新の取得runからこの分数が過ぎるまで、端数を計画に回さない（続きのrunをまとめるための待ち）。0〜1440
    auto_registration: AutoRegistration = field(default_factory=AutoRegistration)  # 登録の自動復旧（既定は無効）。docs/DEVIATIONS.md #102

    def to_dict(self) -> dict:
        d = asdict(self)
        for ln in d["lanes"].values():  # 空のときは項目ごと出さない（従来の設定のsha256・保存済みスナップショットと同一にする）
            if not ln.get("extra_origin_imports"):
                ln.pop("extra_origin_imports", None)
        # 既定のときは出さない（従来の設定のsha256・保存済みスナップショットと同一にする。lanes.extra_origin_importsと同じ作り）
        if not d.get("flush_unfilled_batches"):
            d.pop("flush_unfilled_batches", None)
        if d.get("flush_min_age_minutes") == 30:
            d.pop("flush_min_age_minutes", None)
        # login_wait_seconds（#104）も既定のときは出さない（従来の設定のsha256・保存済みスナップショットと同一にする）
        if (d.get("auto_registration") or {}).get("login_wait_seconds") == AutoRegistration().login_wait_seconds:
            d["auto_registration"].pop("login_wait_seconds", None)
        default_auto = asdict(AutoRegistration())
        default_auto.pop("login_wait_seconds", None)
        if d.get("auto_registration") == default_auto:
            d.pop("auto_registration", None)
        return d

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def for_lane(self, lane: str) -> "WorkflowConfig":
        """1つの市場だけのworkflow用の設定（watchは市場ごとに別のworkflowにする。一方が人手待ちでも他方を止めないため）。"""
        import copy

        c = copy.deepcopy(self)
        c.lanes = {lane: self.lanes[lane]}
        return c

    def page_market(self, lane: str) -> str:
        return self.lanes[lane].page_market or lane

    def needed_sources(self) -> int:
        """1回のworkflowが取得に回せる出典数の目安（ページ上限 / 出典あたりページ）。これを下回るなら候補不足として後退中の出典を戻す。"""
        c = self.completion
        if c.min_selectable > 0:
            return c.min_selectable
        return max(1, -(-self.totals.max_total_pages // max(1, self.limits.pages_per_source)))

    def price_band_for(self, market: str) -> PartitionConfig | None:
        """取得先市場 market の出典の取得に使う価格帯の設定。無効（その市場が markets に無い）なら None。"""
        pb = self.partition.price_band
        if market not in pb.markets:
            return None
        return PartitionConfig(tuple(pb.initial_edges_yen), pb.min_width_yen, pb.max_depth, pb.treat_end_as_depth_limit_pages)

    def _validate_partition(self) -> None:
        pb = self.partition.price_band
        if not isinstance(pb.markets, list) or any(not isinstance(m, str) for m in pb.markets):
            raise ConfigError("partition.price_band.markets は市場名（\"JP\"）のリストで指定してください")
        if not isinstance(pb.initial_edges_yen, list) or any(isinstance(e, bool) or not isinstance(e, int) for e in pb.initial_edges_yen):
            raise ConfigError("partition.price_band.initial_edges_yen は整数（円）のリストで指定してください")
        for name in ("min_width_yen", "max_depth"):
            v = getattr(pb, name)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(f"partition.price_band.{name} は整数で指定してください（指定: {v!r}）")
        try:
            validate_markets(pb.markets, "SELLER")  # USなど、価格帯分割が登録されていない市場は拒否
            PartitionConfig(tuple(pb.initial_edges_yen), pb.min_width_yen, pb.max_depth, pb.treat_end_as_depth_limit_pages)
        except PartitionError as exc:
            raise ConfigError(f"partition.price_band: {exc}") from exc
        if pb.markets and self.close_on_repeated_page is not True:
            raise ConfigError("partition.price_band を使うには close_on_repeated_page=true が必要です（帯の分割は繰り返しページの確認を使います）")

    def validate(self) -> "WorkflowConfig":
        for name in ("submission_enabled", "tool_session_monitor", "close_on_repeated_page"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"{name} はtrue/falseで指定してください")
        self._validate_partition()
        c = self.completion
        for name in ("low_yield_window_runs", "low_yield_window_days", "low_yield_min_pages", "low_yield_window_min_pages", "backoff_start_days",
                     "backoff_max_days", "reserve_unevaluated", "min_selectable"):
            v = getattr(c, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 10000 or (name != "reserve_unevaluated" and name != "min_selectable" and v < 1):
                raise ConfigError(f"completion.{name} は{'0' if name in ('reserve_unevaluated', 'min_selectable') else '1'}以上の整数で指定してください（指定: {v!r}）")
        if not isinstance(c.low_yield_new_rate, (int, float)) or isinstance(c.low_yield_new_rate, bool) or not 0 < c.low_yield_new_rate < 1:
            raise ConfigError("completion.low_yield_new_rate は0より大きく1より小さい数で指定してください")
        if c.backoff_max_days < c.backoff_start_days:
            raise ConfigError("completion.backoff_max_days は backoff_start_days 以上にしてください")
        if not isinstance(c.restart_release_when_scarce, bool):
            raise ConfigError("completion.restart_release_when_scarce はtrue/falseで指定してください")
        if not self.lanes:
            raise ConfigError("lanes（市場ごとの起点レポート）が空です")
        for m, ln in self.lanes.items():
            if m not in MARKETS:
                raise ConfigError(f"lanes のキーは {MARKETS} です: {m!r}")
            if not isinstance(ln.origin_import, int) or ln.origin_import < 1:
                raise ConfigError(f"{m}: origin_import（ACTIVEレポートのimport_id）を明示してください")
            ex = ln.extra_origin_imports
            if not isinstance(ex, list) or any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in ex):
                raise ConfigError(f"{m}: extra_origin_imports は1以上の整数（import_id）のリストで指定してください: {ex!r}")
            if len(set(ex)) != len(ex):
                raise ConfigError(f"{m}: extra_origin_imports に同じimport_idが重複しています: {ex}")
            if ln.origin_import in ex:
                raise ConfigError(f"{m}: extra_origin_imports に origin_import 自身（{ln.origin_import}）は含められません")
            if (ln.page_market or m) not in MARKETS:
                raise ConfigError(f"{m}: page_market は {MARKETS} です")
            if ln.selection_mode not in SELECTION_MODES:
                raise ConfigError(f"{m}: selection_mode は {SELECTION_MODES} から指定してください: {ln.selection_mode!r}")
            if not ln.routes or any(r not in ROUTES for r in ln.routes):
                raise ConfigError(f"{m}: routes は {ROUTES} から指定してください")
            if "BRAND" in ln.routes and not self.brand_enabled:
                raise ConfigError(f"{m}: BRAND は未検証のため brand_enabled=true にするまで使えません（SELLERから開始）")
        lim = self.limits
        for name, cap in (("origin_asins", "origin_asins"), ("sources_per_origin", "sources_per_origin"), ("pages_per_source", "pages_per_source"),
                          ("cross_asins", "cross_asins"), ("cross_sources_per_asin", "cross_sources_per_asin")):
            v = getattr(lim, name)
            if not isinstance(v, int) or v < 1 or v > HARD[cap]:
                raise ConfigError(f"limits.{name} は1〜{HARD[cap]}で指定してください（指定: {v}）")
        if lim.max_asins_per_source is not None and (not isinstance(lim.max_asins_per_source, int) or lim.max_asins_per_source < 1):
            raise ConfigError("limits.max_asins_per_source は1以上の整数またはnull")
        for name in ("max_total_pages", "max_new_sources", "max_submit_asins"):
            v = getattr(self.totals, name)
            if not isinstance(v, int) or v < 1 or v > HARD[name]:
                raise ConfigError(f"totals.{name} は1〜{HARD[name]}で指定してください（指定: {v}）")
        for name in ("max_pages", "max_new_submissions_per_direction", "max_submit_asins"):
            v = getattr(self.daily, name)
            if v is not None and (not isinstance(v, int) or v < 1 or v > 1_000_000):
                raise ConfigError(f"daily.{name} は1以上の整数またはnull（無制限）")
        for name in ("interval_minutes", "failure_retry_minutes", "human_wait_retry_minutes", "notify_minutes"):
            v = getattr(self.watch, name)
            if not isinstance(v, int) or not 1 <= v <= 7 * 24 * 60:
                raise ConfigError(f"watch.{name} は1〜10080（分）")
        for name, lo, hi in (("server_error_streak", 1, 1000), ("new_asin_min_percent", 1, 100)):
            v = getattr(self.stall, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ConfigError(f"stall.{name} は{lo}〜{hi}の整数で指定してください（指定: {v!r}）")
        if not isinstance(self.page_revisit_days, int) or not 1 <= self.page_revisit_days <= 365:
            raise ConfigError("page_revisit_days は1〜365")
        mn = self.totals.max_new_submissions_per_direction
        if mn is not None and (not isinstance(mn, int) or mn < 1 or mn > 100):
            raise ConfigError("totals.max_new_submissions_per_direction は1〜100またはnull")
        if not isinstance(self.tool_max_asins, int) or not 1 <= self.tool_max_asins <= HARD["tool_max_asins"]:
            raise ConfigError(f"tool_max_asins は1〜{HARD['tool_max_asins']}（統合ツールの1回の入力）")
        if not isinstance(self.result_max_age_days, int) or not 1 <= self.result_max_age_days <= 365:
            raise ConfigError("result_max_age_days は1〜365")
        if not isinstance(self.batch_target_asins, int) or not 0 <= self.batch_target_asins <= self.tool_max_asins:
            raise ConfigError("batch_target_asins は0〜tool_max_asinsで指定してください")
        if not isinstance(self.flush_unfilled_batches, bool):
            raise ConfigError("flush_unfilled_batches はtrue/falseで指定してください")
        if isinstance(self.flush_min_age_minutes, bool) or not isinstance(self.flush_min_age_minutes, int) or not 0 <= self.flush_min_age_minutes <= 1440:
            raise ConfigError(f"flush_min_age_minutes は0〜1440（分）の整数で指定してください（指定: {self.flush_min_age_minutes!r}）")
        if self.flush_unfilled_batches and not self.batch_target_asins:
            raise ConfigError("flush_unfilled_batches=true には batch_target_asins>0 が必要です（端数は蓄積方式の中でだけ扱います）")
        self._validate_auto_registration()
        return self

    def _validate_auto_registration(self) -> None:
        a = self.auto_registration
        for name in ("enabled", "continue_past_needs_human"):
            if not isinstance(getattr(a, name), bool):
                raise ConfigError(f"auto_registration.{name} はtrue/falseで指定してください")
        for name, lo, hi in (("post_save_wait_seconds", 0, 600), ("needs_human_after_minutes", 1, 10080), ("pre_save_retry_wait_minutes", 0, 1440),
                             ("pre_save_retry_max_grants", 0, 10), ("loop_interval_minutes", 1, 1440),
                             ("login_wait_seconds", -3600, 3600)):
            v = getattr(a, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ConfigError(f"auto_registration.{name} は{lo}〜{hi}の整数で指定してください（指定: {v!r}）")
        w = a.log_unreadable_waits_seconds
        if not isinstance(w, list) or len(w) > 10 or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 600 for x in w):
            raise ConfigError("auto_registration.log_unreadable_waits_seconds は0〜600秒の整数のリスト（最大10個）で指定してください")


def _partition_from_dict(raw) -> Partition:
    if raw is None:
        return Partition()
    if not isinstance(raw, dict):
        raise ConfigError("partition はオブジェクトで指定してください")
    raw = {k: v for k, v in raw.items() if not k.startswith("_")}
    unknown = set(raw) - {"price_band"}
    if unknown:
        raise ConfigError(f"partition に未知の項目があります: {sorted(unknown)}")
    pb = raw.get("price_band")
    if pb is None:
        return Partition()
    if not isinstance(pb, dict):
        raise ConfigError("partition.price_band はオブジェクトで指定してください")
    try:
        return Partition(price_band=PriceBand(**{k: v for k, v in pb.items() if not k.startswith("_")}))
    except TypeError as exc:
        raise ConfigError(f"partition.price_band に未知の項目があります: {exc}") from exc


def _auto_registration_from_dict(raw) -> AutoRegistration:
    if raw is None:
        return AutoRegistration()
    if not isinstance(raw, dict):
        raise ConfigError("auto_registration はオブジェクトで指定してください")
    return AutoRegistration(**{k: v for k, v in raw.items() if not k.startswith("_")})


def config_from_dict(d: dict) -> WorkflowConfig:
    d = {k: v for k, v in d.items() if not k.startswith("_")}  # "_comment" など説明用のキーは無視する
    try:
        lanes = {m: Lane(**v) for m, v in (d.get("lanes") or {}).items()}
        cfg = WorkflowConfig(
            lanes=lanes, limits=Limits(**d.get("limits", {})), totals=Totals(**d.get("totals", {})), cross=Cross(**d.get("cross", {})),
            partition=_partition_from_dict(d.get("partition")),
            completion=Completion(**{k: v for k, v in (d.get("completion") or {}).items() if not k.startswith("_")}),
            daily=Daily(**d.get("daily", {})), watch=Watch(**d.get("watch", {})), stall=Stall(**d.get("stall", {})),
            auto_registration=_auto_registration_from_dict(d.get("auto_registration")),
            **{k: d[k] for k in ("brand_enabled", "tool_max_asins", "result_max_age_days", "state_dir", "submission_dir",
                                 "page_revisit_days", "profile", "listing_feedback", "skip_failures", "batch_target_asins",
                                 "submission_enabled", "tool_session_monitor", "close_on_repeated_page", "flush_unfilled_batches",
                                 "flush_min_age_minutes") if k in d},
        )
    except TypeError as exc:
        raise ConfigError(f"設定に未知の項目があります: {exc}") from exc
    return cfg.validate()


def load_config(path: str | Path) -> WorkflowConfig:
    try:
        return config_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"設定ファイルを読めません: {path}: {exc}") from exc
