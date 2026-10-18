"""workflow（ひとつの実行入口）の受入テスト: 架空のUS・JPデータ、疑似ブラウザ、使い捨てtest-db。実Amazon・実統合ツールには接続しない。"""
from __future__ import annotations

import dataclasses
import itertools
import json
import random
import re
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from acquisition.lease import NotEligible  # noqa: F401  (import確認)
from asin_collector.source_runner import CaptchaNotSolved, FetchedPage
from tests.integration.acq_helpers import make_active_report
from tests.integration.test_acquisition_runner import results
from tests.unit.test_product_page import product_html
from tool_submission.reconcile import LogEntry
from tool_submission.runner import SubmitFailed, SubmitResponse
from workflow import cli as wcli
from workflow.config import config_from_dict
from workflow.engine import Engine
from workflow.ports import PortBlocked
from workflow.store import WorkflowBusy, WorkflowStore

pytestmark = pytest.mark.integration

CAPTCHA = "Enter the characters you see below"
_ids = itertools.count(random.randint(7_800_000, 7_900_000), 100)


class Crash(BaseException):
    """プロセス死の模擬（Exceptionではないので、エンジンの失敗処理に捕捉されない）。"""


def uid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:9].upper()


@pytest.fixture()
def own(app_role_factory):
    ids = {"US": "AOWNUS000001", "JP": "AOWNJP000001"}
    for m, a in ids.items():
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("INSERT INTO marketplace_account_config (marketplace, account_id, is_active) VALUES (:m, :a, true) "
                              "ON CONFLICT DO NOTHING"), {"m": m, "a": a})
    yield ids
    for m, a in ids.items():
        with app_role_factory.get_session(m) as conn, conn.begin():
            conn.execute(text("DELETE FROM marketplace_account_config WHERE marketplace = :m AND account_id = :a"), {"m": m, "a": a})


class Routing:
    """URLに応じて応答する疑似ブラウザ（商品ページ=販売元、出典のストアフロント=ASIN一覧）。取得したURLを記録する。"""

    def __init__(self, product_seller: dict[str, str], seller_asins: dict[str, list[str]]) -> None:
        self.product_seller, self.seller_asins, self.urls = product_seller, seller_asins, []
        self.captcha_products: set[str] = set()
        self.seller_pages: dict[str, list[list[str]]] = {}
        self.seller_503: set[str] = set()
        self.seller_empty: set[str] = set()  # 「検索結果0件」ウィジェット付きの0件（確認済みの正常な0件）
        self.product_503: set[str] = set()  # 商品ページが5xxを返し続ける
        self.product_offers: dict[str, tuple] = {}
        self.on_fetch = None

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        if self.on_fetch:
            self.on_fetch(url)
        m = re.search(r"/dp/([A-Z0-9]+)", url)
        if m:
            if m.group(1) in self.captcha_products:
                return FetchedPage(f"<html>{CAPTCHA}</html>", url, 200)
            if m.group(1) in self.product_503:
                return FetchedPage('<html><body><div id="cs_503_logo"></div></body></html>', url, 503)
            return FetchedPage(product_html(buybox=self.product_seller.get(m.group(1), "A9NOBODY0001"), offers=self.product_offers.get(m.group(1), ())), url, 200)
        m = re.search(r"me=([A-Z0-9]+)", url)
        if m:
            if m.group(1) in self.seller_503:
                return FetchedPage('<html><body><div id="cs_503_logo"></div></body></html>', url, 503)
            if m.group(1) in self.seller_empty:
                from asin_collector.check_recommendations import NO_RESULTS_MARKER
                return FetchedPage(f'<html><body><div {NO_RESULTS_MARKER}>no results</div></body></html>', url, 200)
            pages = self.seller_pages.get(m.group(1))
            if pages:  # 複数ページの出品者一覧（次ページは &page=N+1）
                pm = re.search(r"[?&]page=(\d+)", url)
                n = int(pm.group(1)) if pm else 1
                base = re.sub(r"&page=\d+", "", url)
                cards = "".join(f'<div data-component-type="s-search-result" data-asin="{a}"></div>' for a in pages[n - 1])
                nxt = f'<a class="s-pagination-next" href="{base}&page={n + 1}">Next</a>' if n < len(pages) else ""
                return FetchedPage(f"<html><body>{cards}{nxt}</body></html>", url, 200)
            return FetchedPage(results(self.seller_asins.get(m.group(1), [])), url, 200)
        raise AssertionError(f"想定外のURL: {url}")

    def wait_for_captcha_solved(self) -> None:
        raise CaptchaNotSolved("CAPTCHAは人が解く（テストでは解かれない）")


def _row_html(rows: list[LogEntry]) -> str:
    out = []
    for e in rows:
        out.append(f'<tr data-seq="{e.seq}" data-type="{e.data_type}" data-country="{e.country}" data-options="{e.options}">'
                   f'<td class="log-type">ASINリスト</td><td class="log-id">{e.seq}</td><td class="log-csv">{e.csv or "-"}</td>'
                   f'<td class="log-status">{e.status}</td><td class="log-memo"><textarea class="memo">{e.memo}</textarea></td>'
                   f'<td class="log-time">-</td></tr>')
    return "<table>" + "".join(out) + "</table>"


class FakeToolPage:
    """疑似の統合ツール画面。状態を持ち、送信・メモ保存・ログ表示に応じて変わる。処理は常に「待機中」のまま。"""

    def __init__(self, n_old: int = 5) -> None:
        base = next(_ids)
        self.rows = [LogEntry(base - 50 + i, "asin_list", "jp", "輸入_OLD", "取得完了", f"{base - 50 + i}_x.csv") for i in range(n_old)]
        self.next = base
        self.submits = 0
        self.memo_fail = False
        self.timeout_after_save = False
        self.retention_log = None
        self.db_rows = None

    def preflight(self):
        return []

    def read_log(self):
        return list(self.rows)

    def read_options(self):
        return {"CNフラグ除外": True, "バリエーション": False}

    def submit(self, item, input_path):
        self.submits += 1
        asins = [a for a in __import__("pathlib").Path(input_path).read_text(encoding="utf-8").split() if a]
        self.rows.append(LogEntry(self.next, "asin_list", "jp", "", "待機中", None, "{cn_safe:on,variation:}"))
        self.next += 2
        if self.timeout_after_save:
            raise SubmitFailed("UNKNOWN", "timeout after SAVE")
        cap = {"endpoint": "./out/start_search4.php", "status": 200, "alerts": [],
               "body": json.dumps({"url": ",".join(asins), "options": {"cn_safe": "on", "variation": ""}, "type": "asin_list", "country": ""}),
               "response": "start_search4asin_listrivalsuccess asin1 https://x.invalid/csv_del.php<br>"}
        return SubmitResponse(evidence=cap)

    def set_memo(self, run_id, memo):
        if self.memo_fail:
            raise RuntimeError("memo save failed")
        self.rows = [dataclasses.replace(e, memo=memo) if e.seq == run_id else e for e in self.rows]

    def log_html(self):
        return _row_html(self.rows)


class FakePorts:
    def __init__(self, routing: dict[str, Routing], tool: FakeToolPage) -> None:
        self.routing, self.tool = routing, tool
        self.blocked_tool: str | None = None
        self.blocked_fetch: dict[str, str] = {}
        self.forbid_all = False

    @contextmanager
    def fetcher(self, market):
        if self.forbid_all:
            raise AssertionError("外部アクセスしてはいけない場面です")
        if market in self.blocked_fetch:
            raise PortBlocked(self.blocked_fetch[market])
        yield self.routing[market]

    @contextmanager
    def tool_page(self):
        if self.forbid_all:
            raise AssertionError("外部アクセスしてはいけない場面です")
        if self.blocked_tool:
            raise PortBlocked(self.blocked_tool)
        yield self.tool


class World:
    """US・JPの架空データ一式（起点ASIN・販売元・出典のASIN）と、設定。"""

    def __init__(self, app_role_factory, tmp_path, *, cfg_extra: dict | None = None, single: bool = False) -> None:
        self.f, self.tmp = app_role_factory, tmp_path
        t = uuid.uuid4().hex[:6].upper()
        self.us_top, self.us_low = f"B0{t}U1", f"B0{t}U2"
        self.jp_top = f"B0{t}J1"
        self.us_seller, self.jp_seller = uid("AUS"), uid("AJP")
        self.us_asins = [f"B0{t}A{i}" for i in range(3)]
        self.jp_asins = [f"B0{t}B{i}" for i in range(3)]
        self.cross_jp_seller, self.cross_us_seller = uid("AXJ"), uid("AXU")
        us_rows = [(self.us_top, 80, 3.0, 9)] + ([] if single else [(self.us_low, 10, 1.0, 2)])
        self.us_import = make_active_report(app_role_factory, tmp_path, "US", us_rows, name="us.csv")
        self.jp_import = make_active_report(app_role_factory, tmp_path, "JP", [(self.jp_top, 60, 2.0, 7)], name="jp.csv")
        self.us = Routing({self.us_top: self.us_seller, self.us_low: "A9SECOND0001", self.us_asins[0]: self.cross_us_seller,
                           self.jp_asins[0]: self.cross_us_seller}, {self.us_seller: self.us_asins})
        self.jp = Routing({self.jp_top: self.jp_seller, self.us_asins[0]: self.cross_jp_seller}, {self.jp_seller: self.jp_asins})
        self.tool = FakeToolPage()
        self.ports = FakePorts({"US": self.us, "JP": self.jp}, self.tool)
        d = {"lanes": {"US": {"origin_import": self.us_import}, "JP": {"origin_import": self.jp_import}},
             "state_dir": str(tmp_path / "wf"), "submission_dir": str(tmp_path / "sub")}
        d.update(cfg_extra or {})
        self.cfg_dict = d
        self.cfg = config_from_dict(d)

    def engine(self, cfg=None) -> Engine:
        cfg = cfg or self.cfg
        return Engine(self.f, self.ports, cfg, WorkflowStore(cfg.state_dir), sleep=lambda s: None)

    def run(self, eng=None) -> Engine:
        eng = eng or self.engine()
        eng.new_state()
        with eng.store.lock(eng.st.workflow_id):
            eng.drive()
        return eng

    def resume(self, eng: Engine, **kw) -> Engine:
        eng2 = self.engine()
        eng2.st = eng.store.load(eng.st.workflow_id)
        with eng2.store.lock(eng2.st.workflow_id):
            eng2.prepare_resume()
            eng2.st.allow_duplicates += kw.get("allow", [])
            eng2.drive()
        return eng2


@pytest.fixture()
def world(app_role_factory, tmp_path, own):
    return World(app_role_factory, tmp_path)


def statuses(eng: Engine) -> dict[str, str]:
    return {t.id: t.status for t in eng.st.tasks.values()}


def submit_tasks(eng):
    return [t for t in eng.st.tasks.values() if t.kind == "submit"]


# ---------------------------------------------------------------- 一連の流れ

def test_full_flow_ends_at_link_record_even_though_the_tool_is_still_waiting(world):
    eng = world.run()
    s = statuses(eng)
    assert s["discover:US"] == s["acquire:US"] == s["discover:JP"] == s["acquire:JP"] == "DONE", s
    subs = submit_tasks(eng)
    assert {t.result["direction"] for t in subs} == {"EXPORT", "IMPORT"}  # 業務方向は起点レポートの市場（取得先市場とは別）
    assert all(t.status == "DONE" and t.result["tool_run_id"] for t in subs), [(t.id, t.status, t.detail) for t in subs]
    assert wcli.overall(eng.st) == "COMPLETED"  # 統合ツールは「待機中」のまま（処理完了は待たない）
    assert all(e.status == "待機中" for e in world.tool.rows[-2:])
    assert world.tool.submits == 2
    # 注文点数の最も多い起点だけが調べられた（初期値: 起点1ASIN）。自社ID・Amazon小売は登録しない
    assert any(f"/dp/{world.us_top}" in u for u in world.us.urls) and not any(f"/dp/{world.us_low}" in u for u in world.us.urls)
    # 実行ID・出典・取得ASINの紐付け: 既存のtool_execution_linkに、業務方向の市場で記録される
    exp = next(t for t in subs if t.result["direction"] == "EXPORT")
    with world.f.get_session("US") as conn, conn.begin():
        row = conn.execute(text("SELECT seller_id FROM tool_execution_link WHERE marketplace='US' AND tool_run_id=:r"),
                           {"r": exp.result["tool_run_id"]}).one()
    assert row.seller_id == world.us_seller
    # 反対側で見つけた出典は登録だけ（初期値: 自動取得OFF）。JP側の取得はJPレーンの出典だけ
    cross = eng.st.tasks["cross:US"]
    assert cross.status == "DONE" and len(cross.result["new_sources"]) == 1
    assert not any(world.cross_jp_seller in u for u in world.jp.urls)


def test_a_second_run_moves_on_to_the_next_candidate_and_never_resends_completed_submissions(world):
    low_seller = "A9SECOND0001"
    world.us.seller_asins[low_seller] = [f"B0{uuid.uuid4().hex[:8].upper()}"[:10] for _ in range(2)]
    first = world.run()
    n_tool, n_us = world.tool.submits, len(world.us.urls)
    second = world.run()
    assert wcli.overall(second.st) == "COMPLETED"
    picked = [p["asin"] for p in second.st.tasks["discover:US"].result["selection"]["picked"]]
    assert picked == [world.us_low]  # 完了済みの上位候補(us_top)を選び直さず、次の候補へ進む
    assert not any(f"/dp/{world.us_top}" in u for u in world.us.urls[n_us:])  # 調査済みの商品ページへ再アクセスしない
    assert not any(world.us_seller in u for u in world.us.urls[n_us:])       # 取得済みの出典を再取得しない
    keys1 = {t.result["key"] for t in submit_tasks(first)}
    keys2 = {t.result["key"] for t in submit_tasks(second)}
    assert keys1.isdisjoint(keys2) and len(keys2) == 1  # 前回の投入は繰り返さず、次の候補の分だけを新しく投入
    assert world.tool.submits == n_tool + 1
    assert second.st.tasks["discover:JP"].result["selection"]["picked"] == []  # JPの候補は処理済み


def test_limits_are_applied_and_totals_stop_the_run_with_a_hold(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra={"totals": {"max_total_pages": 1, "max_new_sources": 1, "max_submit_asins": 2}})
    eng = w.run()
    assert len(eng.st.tasks["discover:US"].result["new_sources"]) == 1
    # 新規出典の合計上限(1)に達したので、JPの発見は保留（USの独立した工程は進む）
    assert eng.st.tasks["discover:JP"].status == "HELD" and "上限" in eng.st.tasks["discover:JP"].detail
    # 取得ページの合計上限(1)は商品ページの調査で使い切るので、出典の取得は保留（再利用できる結果も無い）
    assert eng.st.tasks["acquire:US"].status == "HELD"
    assert all("上限" in r["reason"] for r in eng.st.tasks["acquire:US"].result["sources"].values())
    # 投入ASIN合計の上限(2)に対し、3件のrunは投入しない
    assert all(t.status == "HELD" and "上限" in t.detail for t in submit_tasks(eng))
    assert w.tool.submits == 0


def test_one_market_waiting_does_not_stop_the_independent_other_market(world):
    world.jp.captcha_products = {world.jp_top}
    eng = world.run()
    s = statuses(eng)
    assert s["discover:JP"] == "WAITING" and "CAPTCHA" in eng.st.tasks["discover:JP"].detail
    assert s["acquire:JP"] == "PENDING" and s["discover:US"] == s["acquire:US"] == "DONE"
    us_sub = [t for t in submit_tasks(eng) if t.lane == "US"]
    assert us_sub and all(t.status == "DONE" for t in us_sub)
    assert wcli.overall(eng.st) == "NEEDS_ACTION"
    # CAPTCHAを解いた後のresumeで、残りだけが進む（USの完了工程は繰り返さない）
    world.jp.captcha_products = set()
    n_us = len(world.us.urls)
    eng2 = world.resume(eng)
    assert statuses(eng2)["discover:JP"] == "DONE" and wcli.overall(eng2.st) == "COMPLETED"
    assert len(world.us.urls) - n_us <= 1  # JPレーンの反対側展開(USの商品ページ)だけ


def test_concurrent_resume_is_refused_and_the_lock_is_released_afterwards(world):
    eng = world.run()
    store = eng.store
    with store.lock(eng.st.workflow_id):
        with pytest.raises(WorkflowBusy):
            with store.lock(eng.st.workflow_id):
                pass
        rc = wcli.main(["resume", "--id", eng.st.workflow_id, "--state-dir", world.cfg.state_dir], factory=world.f, ports=world.ports)
        assert rc == 2
    with store.lock(eng.st.workflow_id):
        pass


# ---------------------------------------------------------------- 投入の中断・失敗からの再開（再送しない）

def test_process_death_after_save_does_not_resend(world):
    eng = world.engine()
    eng.new_state()
    real_save = eng.store.save
    calls = {"after_submit_failures": 0}

    def flaky_save(st):
        if world.tool.submits >= 1 and calls["after_submit_failures"] < 1:
            calls["after_submit_failures"] += 1
            raise Crash("process died after SAVE (simulated)")  # SAVE後に状態保存へ失敗（プロセス死）
        return real_save(st)

    eng.store.save = flaky_save
    with pytest.raises(Crash):
        with eng.store.lock(eng.st.workflow_id):
            eng.drive()
    assert world.tool.submits == 1
    eng.store.save = real_save
    eng2 = world.resume(eng)
    assert wcli.overall(eng2.st) == "COMPLETED"
    assert world.tool.submits == 2  # 1件目は再送されず、2件目だけが送られた


def test_timeout_after_save_is_unknown_and_resume_reconciles_without_resending(world):
    world.tool.timeout_after_save = True
    eng = world.run()
    unknown = [t for t in submit_tasks(eng) if t.status == "UNKNOWN"]
    assert len(unknown) == 1 and world.tool.submits == 1
    # 同時に未完了の別の投入があるので、他の投入は保留（完了・解決するまで次へ進まない）
    assert any(t.status == "HELD" and "未完了" in t.detail for t in submit_tasks(eng))
    world.tool.timeout_after_save = False
    eng2 = world.resume(eng)
    assert world.tool.submits == 1  # 結果不明の間はSAVEを再送しない
    assert any(t.status == "UNKNOWN" for t in submit_tasks(eng2))  # 根拠が無いので新規行を自分のものと断定しない・メモも付けない
    assert all(e.memo != "輸出_" + world.us_seller and e.memo != "輸入_" + world.jp_seller for e in world.tool.rows[-2:])


def test_memo_save_failure_resumes_from_the_memo_step_only(world):
    world.tool.memo_fail = True
    eng = world.run()
    failed = [t for t in submit_tasks(eng) if t.status == "FAILED"]
    assert failed and "メモ" in failed[0].detail and world.tool.submits == 1
    world.tool.memo_fail = False
    eng2 = world.resume(eng)
    assert wcli.overall(eng2.st) == "COMPLETED"
    assert world.tool.submits == 2  # ASINの再投入は無い（特定済みの実行IDのメモ保存から再開）


def test_db_record_failure_resumes_from_the_link_step_only(world, monkeypatch):
    import workflow.engine as E

    real = E.ingest_one_row
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("db down (simulated)")
        return real(*a, **k)

    monkeypatch.setattr(E, "ingest_one_row", flaky)
    eng = world.run()
    failed = [t for t in submit_tasks(eng) if t.status == "FAILED"]
    assert len(failed) == 1 and "対応記録" in failed[0].detail
    eng2 = world.resume(eng)
    assert wcli.overall(eng2.st) == "COMPLETED" and world.tool.submits == 2


def test_ingest_login_wait_then_resume_sends_after_login(world):
    world.ports.blocked_tool = "統合ツールにログインしてください"
    eng = world.run()
    assert all(t.status == "WAITING" and "ログイン" in t.detail for t in submit_tasks(eng))
    assert world.tool.submits == 0 and statuses(eng)["acquire:US"] == "DONE"
    world.ports.blocked_tool = None
    eng2 = world.resume(eng)
    assert wcli.overall(eng2.st) == "COMPLETED" and world.tool.submits == 2


# ---------------------------------------------------------------- 重複・保全・過去のimport

def test_same_asin_set_from_a_different_run_is_held_as_a_duplicate_candidate_and_can_be_allowed(app_role_factory, tmp_path, own):
    world = World(app_role_factory, tmp_path, single=True)
    first = world.run()
    n = world.tool.submits
    # 別のrun（別の出典・別のキー）で同じASIN集合・同じ方向の投入計画を作る
    t_us = next(t for t in submit_tasks(first) if t.lane == "US")
    from tool_submission.ledger import SubmissionLedger
    from tool_submission.model import item_from_manifest

    led = SubmissionLedger(world.tmp / "sub" / "ledger.jsonl")
    m = dict(led.state(t_us.result["key"]).manifest)
    m.update(key="dupkey000000001", collector_run_id=m["collector_run_id"] + 100000, input_sha256=m["input_sha256"])
    led.append("dupkey000000001", "PLANNED", manifest=m)
    from workflow.engine import find_duplicates

    dups = find_duplicates(led, item_from_manifest(m))
    assert dups and dups[0]["key"] == t_us.result["key"]
    # workflow上でも保留になり、理由が表示される。許可すれば進む
    eng = world.engine()
    eng.new_state()
    from workflow.store import Task

    eng.st.tasks["submit:dup"] = Task("submit:dup", "submit", "US", result={"key": "dupkey000000001", "asin_count": m["asin_count"]})
    with eng.store.lock(eng.st.workflow_id):
        eng.drive()
    t = eng.st.tasks["submit:dup"]
    assert t.status == "HELD" and "重複候補" in t.detail and t_us.result["key"] in t.detail
    assert world.tool.submits == n


def test_log_limit_blocks_a_new_send_when_the_oldest_output_is_not_preserved(world):
    world.tool.rows = [LogEntry(8_000_000 + i, "asin_list", "jp", "輸入_X", "待機中", None) for i in range(300)]
    eng = world.run()
    subs = submit_tasks(eng)
    assert subs and all(t.status == "HELD" and "300" in t.detail for t in subs)
    assert world.tool.submits == 0


def test_only_the_configured_origin_import_is_used(world, app_role_factory, tmp_path):
    older = make_active_report(app_role_factory, tmp_path, "US", [("B0OLDER001", 99, 9.0, 99)], name="old_us.csv")
    assert older != world.us_import
    eng = world.run()
    assert not any("B0OLDER001" in u for u in world.us.urls)  # 設定に無い過去のimportは混ぜない
    assert all(p.get("origin_import_id") in (world.us_import, world.jp_import)
               for t in submit_tasks(eng) for p in json.loads(json.dumps(
                   __import__("tool_submission.ledger", fromlist=["x"]).SubmissionLedger(world.tmp / "sub" / "ledger.jsonl")
                   .state(t.result["key"]).manifest["paths"])))


# ---------------------------------------------------------------- plan・status・CLI・バックアップ

def test_plan_makes_no_external_access_and_no_db_updates_and_labels_estimates(world, capsys, tmp_path):
    cfgp = tmp_path / "cfg.json"
    cfgp.write_text(json.dumps(world.cfg_dict), encoding="utf-8")
    world.ports.forbid_all = True
    with world.f.get_session("US") as conn, conn.begin():
        before = conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM acquisition_source)")).one()
    rc = wcli.main(["plan", "--config", str(cfgp)], factory=world.f, ports=world.ports)
    out = capsys.readouterr().out
    assert rc == 0 and "見積り・上限" in out and "確定値ではありません" in out and world.us_top in out
    with world.f.get_session("US") as conn, conn.begin():
        after = conn.execute(text("SELECT (SELECT count(*) FROM collector_run), (SELECT count(*) FROM acquisition_source)")).one()
    assert tuple(before) == tuple(after)
    assert not (tmp_path / "wf").exists()  # 状態も作らない


def test_status_command_prints_per_stage_counts_and_run_exit_code(world, capsys, tmp_path):
    cfgp = tmp_path / "cfg.json"
    cfgp.write_text(json.dumps(world.cfg_dict), encoding="utf-8")
    rc = wcli.main(["run", "--config", str(cfgp)], factory=world.f, ports=world.ports)
    assert rc == 0
    capsys.readouterr()
    assert wcli.main(["status", "--state-dir", world.cfg.state_dir]) == 0
    out = capsys.readouterr().out
    assert "全体: COMPLETED" in out and "投入" in out and "対応記録済みの投入" in out and "処理完了" in out


def test_backup_and_restore_keep_completed_stages_from_running_again(world, tmp_path, capsys):
    eng = world.run()
    wid = eng.st.workflow_id
    zp = tmp_path / "bk" / "wf.zip"
    assert wcli.main(["backup", "--state-dir", world.cfg.state_dir, "--submission-dir", world.cfg.submission_dir, "--out", str(zp)]) == 0
    # 状態・台帳を失った環境へ復元する
    new_wf, new_sub = tmp_path / "wf2", tmp_path / "sub2"
    assert wcli.main(["restore", "--zip", str(zp), "--state-dir", str(new_wf), "--submission-dir", str(new_sub)]) == 0
    assert wcli.main(["restore", "--zip", str(zp), "--state-dir", str(new_wf), "--submission-dir", str(new_sub)]) == 2  # 既存は上書きしない
    cfg2 = config_from_dict({**world.cfg_dict, "state_dir": str(new_wf), "submission_dir": str(new_sub)})
    # 復元後のマニフェストの入力ファイルのパスは元の場所を指す（実運用では同じ場所へ復元する）。台帳の完了状態が使われる。
    world.ports.forbid_all = True
    n = world.tool.submits
    store = WorkflowStore(new_wf)
    eng2 = Engine(world.f, world.ports, cfg2, store, sleep=lambda s: None)
    eng2.st = store.load(wid)
    with store.lock(wid):
        eng2.prepare_resume()
        eng2.drive()
    assert wcli.overall(eng2.st) == "COMPLETED" and world.tool.submits == n
    # 破損したバックアップは検証で拒否される
    bad = tmp_path / "bad.zip"
    bad.write_bytes(zp.read_bytes()[:-30])
    assert wcli.main(["restore", "--zip", str(bad), "--state-dir", str(tmp_path / "x"), "--submission-dir", str(tmp_path / "y")]) == 2
    assert not (tmp_path / "x").exists()  # 検証に失敗したら何も書かない


def test_brand_routes_are_rejected_until_enabled_and_hard_caps_apply():
    from workflow.config import ConfigError

    base = {"lanes": {"US": {"origin_import": 1, "routes": ["BRAND"]}}}
    with pytest.raises(ConfigError, match="brand_enabled"):
        config_from_dict(base)
    config_from_dict({**base, "brand_enabled": True})
    with pytest.raises(ConfigError, match="origin_asins"):
        config_from_dict({"lanes": {"US": {"origin_import": 1}}, "limits": {"origin_asins": 99}})
    with pytest.raises(ConfigError, match="lanes"):
        config_from_dict({"lanes": {"XX": {"origin_import": 1}}})


def test_acquire_crash_marks_failed_and_resume_continues(world, monkeypatch):
    import workflow.engine as E

    real = E.run_batch
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("browser crashed (simulated)")
        return real(*a, **k)

    monkeypatch.setattr(E, "run_batch", boom)
    eng = world.run()
    assert statuses(eng)["acquire:US"] == "FAILED"
    eng2 = world.resume(eng)
    assert statuses(eng2)["acquire:US"] == "DONE" and wcli.overall(eng2.st) == "COMPLETED", [(t.id, t.status, t.detail) for t in eng2.st.tasks.values() if t.status != "DONE"]


# ---------------------------------------------------------------- 自社除外・Amazon小売・実行中の出典・lease失効

def test_own_seller_is_excluded_and_amazon_retail_is_held_without_registering(world):
    world.us.product_seller[world.us_top] = "AOWNUS000001"  # 自社
    eng = world.run()
    d = eng.st.tasks["discover:US"]
    assert d.status == "DONE" and d.result["sources"] == [] and "起点1件" in d.detail
    assert eng.st.tasks["acquire:US"].detail == "取得する出典がありません"
    assert not any(t.lane == "US" for t in submit_tasks(eng))
    world.us.product_seller[world.us_low] = "ATVPDKIKX0DER"  # Amazon小売かの判定ができないID → 登録せず報告
    eng2 = world.engine(config_from_dict({**world.cfg_dict, "limits": {"origin_asins": 2}}))
    eng2.new_state()
    with eng2.store.lock(eng2.st.workflow_id):
        eng2.drive()
    held = eng2.st.tasks["discover:US"].result["held"]
    assert [h["key"] for h in held] == ["ATVPDKIKX0DER"] and eng2.st.tasks["discover:US"].result["sources"] == []
    assert "Amazon小売" in "\n".join(wcli.status_lines(eng2.st))


def _pre_claimed_source(world, app_role_factory):
    from acquisition.lease import AcquisitionRunService
    from acquisition.registry import SourceRegistry

    rp = SourceRegistry(app_role_factory, actor="tester").register_path("US", "SELLER", world.us_seller, "", origin_import_id=world.us_import,
                                                                       origin_asin=world.us_top)
    claim = AcquisitionRunService(app_role_factory, actor="tester").claim("US", rp.source.source_id)
    return rp.source.source_id, claim


def test_a_source_running_elsewhere_is_not_fetched_twice_and_an_expired_lease_is_reaped_on_resume(world, app_role_factory):
    from tests.integration.acq_helpers import expire_run

    sid, claim = _pre_claimed_source(world, app_role_factory)  # 別プロセスが取得中（leaseが有効）
    eng = world.run()
    acq = eng.st.tasks["acquire:US"]
    assert acq.status == "HELD" and "実行中" in acq.detail or "RUNNING" in acq.detail or acq.result["sources"][str(sid)]["mode"] == "HELD"
    assert not any(world.us_seller in u for u in world.us.urls)  # 二重に取得しない
    assert not any(t.lane == "US" for t in submit_tasks(eng))
    assert statuses(eng)["acquire:JP"] == "DONE"  # 独立したJPは進む
    expire_run(app_role_factory, "US", claim.run_id)  # leaseが切れた（持ち主が死んだ）
    eng2 = world.resume(eng)
    assert statuses(eng2)["acquire:US"] == "DONE"  # 期限切れを回収して、取得できる
    assert any(world.us_seller in u for u in world.us.urls)
    assert wcli.overall(eng2.st) == "COMPLETED"


def test_status_text_names_what_to_do_and_how_to_resume(world, capsys):
    world.ports.blocked_tool = "統合ツールにログインしてください"
    eng = world.run()
    text_ = "\n".join(wcli.status_lines(eng.st))
    assert "待機（人の操作）" in text_ and "resume" in text_ and "ログイン" in text_ and "完了条件に含みません" in text_


# ---------------------------------------------------------------- 既存結果の引き継ぎ・外部操作ゼロ・新規投入の上限

def test_a_fresh_workflow_with_no_remaining_candidates_makes_zero_external_operations(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, single=True)
    first = w.run()
    n_tool = w.tool.submits
    w.ports.forbid_all = True  # ブラウザ・統合ツールに一切触れてはいけない
    cfg2 = config_from_dict({**w.cfg_dict, "state_dir": str(tmp_path / "wf_fresh")})  # 空のworkflow状態（投入台帳・調査履歴は既存のまま）
    second = w.run(w.engine(cfg2))
    assert wcli.overall(second.st) == "COMPLETED"
    from workflow.store import usage

    u = usage(second.st)
    assert u["pages"] == 0 and u["new_sources"] == 0 and u["new_submissions"] == 0
    assert not submit_tasks(second) and w.tool.submits == n_tool
    from tool_submission.ledger import SubmissionLedger

    led = SubmissionLedger(tmp_path / "sub" / "ledger.jsonl")
    assert all(led.state(t.result["key"]).state == "LINKED" for t in submit_tasks(first))


def test_new_submissions_per_direction_are_capped_and_the_cap_survives_resume(world):
    second_seller = uid("AUS")
    world.us.product_offers = {world.us_top: (second_seller,)}
    world.us.seller_asins[second_seller] = [f"B0{uuid.uuid4().hex[:8].upper()}"[:10] for _ in range(2)]
    eng = world.run()
    us = [t for t in submit_tasks(eng) if t.lane == "US"]
    assert len(us) == 2 and sorted(t.status for t in us) == ["DONE", "HELD"], [(t.id, t.status, t.detail) for t in us]
    held = next(t for t in us if t.status == "HELD")
    assert "新しく投入できる件数" in held.detail
    n = world.tool.submits
    eng2 = world.resume(eng)  # 上限はresumeでリセットされない
    assert world.tool.submits == n
    assert next(t for t in submit_tasks(eng2) if t.id == held.id).status == "HELD"


def test_product_page_investigations_count_against_the_amazon_page_cap(app_role_factory, tmp_path, own):
    w = World(app_role_factory, tmp_path, cfg_extra={"totals": {"max_total_pages": 1}, "cross": {"enabled": False}})
    eng = w.run()
    from workflow.store import usage

    assert usage(eng.st)["product_pages"] == 1  # 商品ページ1件で上限に達する
    assert eng.st.tasks["acquire:US"].status == "HELD" or all(r["mode"] != "FETCHED" for r in eng.st.tasks["acquire:US"].result["sources"].values())
    assert usage(eng.st)["pages"] <= 1
