"""取得runから統合ツール用の入力ファイルと投入計画を作る処理の結合テスト（専用test-db。DBは読み取りだけ。統合ツールには接続しない）。"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from acquisition.registry import SourceRegistry
from asin_collector.source_runner import PageLoadError, run_claimed_source
from tests.integration.acq_helpers import make_active_report, make_runs, unique_key
from tests.integration.test_acquisition_runner import FakeFetcher, results
from tool_submission.cli import main as cli_main
from tool_submission.ledger import SubmissionLedger
from tool_submission.model import SubmissionError
from tool_submission.planner import build_items, write_plan

pytestmark = pytest.mark.integration


@pytest.fixture()
def registry(app_role_factory):
    return SourceRegistry(app_role_factory, actor="tester")


@pytest.fixture()
def runs(app_role_factory):
    return make_runs(app_role_factory)


def asin(n):
    return f"B0SUB{n:05d}"


def fetched(app_role_factory, registry, runs, tmp_path, market, pages, *, origin_asin, units, name, via=None, key=None, limits=None):
    """起点つき（または経由）の出典を作り、架空の取得を完了させて (出典, run_id, 結果) を返す。"""
    rid = make_active_report(app_role_factory, tmp_path, market, [(origin_asin, 50, 3.0, units)], name=name)
    key = key or unique_key("ASUB")
    rp = registry.register_path(market, "SELLER", key, origin_import_id=rid, origin_asin=origin_asin)
    claim = runs.claim(market, rp.source.source_id)
    rep = run_claimed_source(runs, app_role_factory, FakeFetcher(*pages), claim, sleep=lambda s: None, limits=limits)
    return rp.source, claim.run_id, rep, rid, key


def test_export_source_from_a_us_report_gets_an_export_input_and_the_partial_marker(app_role_factory, registry, runs, tmp_path):
    src, run_id, rep, rid, key = fetched(app_role_factory, registry, runs, tmp_path, "US",
                                         [results([asin(1), asin(2)], "https://www.amazon.com/s?p=2"), results([asin(3)])],
                                         origin_asin=asin(100), units=9, name="a.csv")
    assert rep.outcome == "SUCCESS_NONEMPTY"
    items, files = build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=tmp_path / "out")
    (it,) = items
    assert (it.direction, it.business_market, it.fetch_market, it.source_id, it.collector_run_id) == ("EXPORT", "US", "US", src.source_id, run_id)
    assert it.memo == f"輸出_{key.upper()}" and it.status == "DRAFT" and it.tool_country is None  # ツールの国設定は自動で決めない
    assert files[0] == f"{asin(1)}\n{asin(2)}\n{asin(3)}\n".encode() and it.asin_count == 3
    assert it.input_sha256 == hashlib.sha256(files[0]).hexdigest()
    assert it.origin == {"market": "US", "import_id": rid, "asin": asin(100), "units": 9}


def test_partial_flag_for_truncated_runs_and_unusable_runs_are_refused(app_role_factory, registry, runs, tmp_path):
    from asin_collector.source_runner import RunLimits

    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "JP", [results([asin(11)], "https://www.amazon.co.jp/s?p=2")],
                                   origin_asin=asin(101), units=2, name="b.csv", limits=RunLimits(max_pages=1))
    assert rep.outcome == "SUCCESS_TRUNCATED"
    (it,), _ = build_items(app_role_factory, "JP", run_id, tool_country="jp", out_dir=tmp_path / "out")
    assert (it.direction, it.business_market, it.memo.startswith("輸入_"), it.status, it.tool_country) == ("IMPORT", "JP", True, "READY", "jp")
    assert it.input_is_partial is True and "一部のASIN" in it.partial_reason and it.run_outcome == "SUCCESS_TRUNCATED"  # 部分取得からの入力を明示
    # 部分取得(PARTIAL)・失敗の実行、他市場のrunは入力にしない
    rid2 = make_active_report(app_role_factory, tmp_path, "JP", [(asin(102), 5, 1.0, 1)], name="c.csv")
    rp = registry.register_path("JP", "SELLER", unique_key("APART"), origin_import_id=rid2, origin_asin=asin(102))
    claim = runs.claim("JP", rp.source.source_id)
    bad = run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(13)], "https://www.amazon.co.jp/s?p=2"), PageLoadError("x")), claim, sleep=lambda s: None)
    assert bad.outcome == "PARTIAL"
    with pytest.raises(SubmissionError, match="入力に使えません"):
        build_items(app_role_factory, "JP", claim.run_id, tool_country=None, out_dir=tmp_path / "out")
    with pytest.raises(SubmissionError, match="存在しません"):
        build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=tmp_path / "out")  # JPのrunをUSとして指定
    with pytest.raises(SubmissionError, match="jp / us"):
        build_items(app_role_factory, "JP", run_id, tool_country="xx", out_dir=tmp_path / "out")


def test_direction_comes_from_the_discovery_path_not_from_the_fetch_market(app_role_factory, registry, runs, tmp_path):
    # JPで取得した出典でも、経路が輸出(US起点)なら EXPORT。取得先市場から方向を決めない
    us_rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(103), 80, 4.0, 7)], name="d.csv")
    via_key = unique_key("AVIA")
    via = registry.register_path("US", "SELLER", via_key, origin_import_id=us_rid, origin_asin=asin(103))
    claim0 = runs.claim("US", via.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(14)])), claim0, sleep=lambda s: None)
    key = unique_key("AXJP")
    registry.register_via_path("JP", "SELLER", key, via_market="US", via_source_id=via.source.source_id, via_asin=asin(14),
                               allowed_origin_import_ids=[us_rid])
    jp_src = _source_id(app_role_factory, "JP", key)
    claim = runs.claim("JP", jp_src)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(15), asin(16)])), claim, sleep=lambda s: None)
    (it,), _ = build_items(app_role_factory, "JP", claim.run_id, tool_country="us", out_dir=tmp_path / "out")
    assert (it.fetch_market, it.direction, it.business_market, it.memo.startswith("輸出_")) == ("JP", "EXPORT", "US", True)
    assert [(p["stage"], p["direction"], p["via_source_id"], p["via_asin"], p["origin_import_id"], p["origin_units"]) for p in it.paths] == [
        ("VIA_SOURCE", "EXPORT", via.source.source_id, asin(14), us_rid, 7)]  # 経由出典・経由ASIN・起点の注文実績まで残る


def _source_id(factory, market, key):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT source_id FROM acquisition_source WHERE key_primary = :k"), {"k": key.upper()}).scalar_one()


def test_one_run_used_in_two_directions_makes_two_separate_inputs_with_distinct_keys(app_role_factory, registry, runs, tmp_path):
    us_rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(104), 80, 4.0, 7)], name="e1.csv")
    jp_rid = make_active_report(app_role_factory, tmp_path, "JP", [(asin(105), 80, 4.0, 3)], name="e2.csv")
    key = unique_key("ABOTH")
    registry.register_path("US", "SELLER", key, origin_import_id=us_rid, origin_asin=asin(104))
    rp = registry.register_path("US", "SELLER", key, origin_import_id=jp_rid, origin_asin=asin(105))
    claim = runs.claim("US", rp.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(21), asin(22)])), claim, sleep=lambda s: None)
    items, files = build_items(app_role_factory, "US", claim.run_id, tool_country="jp", out_dir=tmp_path / "out")
    assert sorted(i.direction for i in items) == ["EXPORT", "IMPORT"]
    ex, im = sorted(items, key=lambda i: i.direction)
    assert ex.key != im.key and ex.input_file != im.input_file and ex.memo.startswith("輸出_") and im.memo.startswith("輸入_")
    assert ex.other_directions == ["IMPORT"] and im.other_directions == ["EXPORT"]
    assert (ex.origin["units"], im.origin["units"]) == (7, 3) and files[0] == files[1]  # 同じASINでも方向ごとに別の入力・別の対応


def test_source_without_a_direction_needs_an_explicit_direction_that_matches_the_paths(app_role_factory, registry, runs, tmp_path):
    key = unique_key("AMAN")
    rp = registry.register_path("US", "SELLER", key)  # 手動登録（起点なし）
    claim = runs.claim("US", rp.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(31)])), claim, sleep=lambda s: None)
    with pytest.raises(SubmissionError, match="--direction"):
        build_items(app_role_factory, "US", claim.run_id, tool_country=None, out_dir=tmp_path / "out")
    (it,), _ = build_items(app_role_factory, "US", claim.run_id, tool_country=None, out_dir=tmp_path / "out", direction_override="IMPORT")
    assert (it.direction, it.origin) == ("IMPORT", None)
    # 経路に方向がある出典へ、異なる方向は指定できない
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(32)])], origin_asin=asin(106), units=1, name="f.csv")
    with pytest.raises(SubmissionError, match="経路の方向"):
        build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=tmp_path / "out", direction_override="IMPORT")


def test_duplicate_rows_are_removed_in_the_file_but_the_evidence_counts_are_kept(app_role_factory, registry, runs, tmp_path):
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(41), asin(42)])], origin_asin=asin(107), units=1, name="g.csv")
    with app_role_factory.get_session("US") as conn, conn.begin():  # 追記専用テーブルへの追記（重複行を人工的に作る）
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :r, :a)"), {"r": run_id, "a": asin(41).lower()})
    (it,), (content,) = build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=tmp_path / "out")
    assert content == f"{asin(41)}\n{asin(42)}\n".encode()
    assert (it.total_rows, it.asin_count, it.duplicate_rows_removed) == (3, 2, 1)  # ASINの重複除外と、証拠(行数)の保持は別


def test_write_plan_is_idempotent_updates_only_before_submission_and_never_overwrites_files(app_role_factory, registry, runs, tmp_path):
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(51), asin(52)])], origin_asin=asin(108), units=4, name="h.csv")
    out = tmp_path / "out"
    ledger = SubmissionLedger(out / "ledger.jsonl")
    items, files = build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=out)
    assert [h for _, h in write_plan(items, files, ledger)] == ["新規"]
    items2, files2 = build_items(app_role_factory, "US", run_id, tool_country=None, out_dir=out)
    assert [h for _, h in write_plan(items2, files2, ledger)] == ["変更なし"] and len(ledger.events()) == 1  # 再実行で二重記録しない
    key = items[0].key
    items3, _ = build_items(app_role_factory, "US", run_id, tool_country="jp", out_dir=out)
    assert [h for _, h in write_plan(items3, files, ledger)] == ["更新"] and ledger.state(key).manifest["tool_country"] == "jp"
    assert json.loads((out / f"manifest_{key}.json").read_text(encoding="utf-8"))["status"] == "READY"
    ledger.append(key, "SUBMIT_STARTED", before_run_ids=[], attempt=1)
    items4, _ = build_items(app_role_factory, "US", run_id, tool_country="us", out_dir=out)
    how = write_plan(items4, files, ledger)[0][1]
    assert "SUBMITTING" in how and ledger.state(key).manifest["tool_country"] == "jp"  # 投入が始まった後は計画を書き換えない
    (out / f"run{run_id}_EXPORT_{key}.csv").write_bytes(b"tampered\n")
    with pytest.raises(SubmissionError, match="上書きしません"):
        write_plan(items, files, ledger)


def test_cli_plan_dry_run_writes_nothing_and_reconcile_confirms_from_a_saved_log(app_role_factory, registry, runs, tmp_path, monkeypatch, capsys):
    from db.session import SessionFactory

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    _, run_id, _, _, key = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(61)])], origin_asin=asin(109), units=2, name="i.csv")
    out, led = tmp_path / "o", tmp_path / "o" / "l.jsonl"
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--tool-country", "jp", "--out-dir", str(out), "--ledger", str(led), "--dry-run"]) == 0
    assert not out.exists() and "dry-run" in capsys.readouterr().out
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--out-dir", str(out), "--ledger", str(led)]) == 0
    assert "国設定が未確定" in capsys.readouterr().out
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--tool-country", "jp", "--out-dir", str(out), "--ledger", str(led)]) == 0
    ledger = SubmissionLedger(led)
    (k,) = ledger.all_states()
    html = tmp_path / "log.html"
    memo = f"輸出_{key.upper()}"
    row = ('<tr data-seq="{s}" data-type="asin_list" data-text="asin" data-country="jp"><td class="log-status">待機中</td>'
           '<td class="log-csv">-</td><td><textarea class="memo">{m}</textarea></td></tr>')
    html.write_text("<table>" + row.format(s=7400001, m=memo) + "</table>", encoding="utf-8")
    assert cli_main(["reconcile", "--key", k, "--log-html", str(html), "--ledger", str(led)]) == 0
    assert ledger.state(k).tool_run_id == "7400001"
    html.write_text("<table>" + row.format(s=7400001, m=memo) + row.format(s=7400002, m=memo) + "</table>", encoding="utf-8")
    assert cli_main(["reconcile", "--key", k, "--log-html", str(html), "--ledger", str(led)]) == 0  # 確定済みは変えない
    assert ledger.state(k).tool_run_id == "7400001"


def test_prior_tool_runs_with_the_same_memo_are_listed_and_never_adopted_as_the_new_run(app_role_factory, registry, runs, tmp_path, monkeypatch):
    from db.session import SessionFactory

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    _, run_id, _, _, key = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(71)])], origin_asin=asin(110), units=2, name="j.csv")
    with app_role_factory.get_session("US") as conn, conn.begin():  # 過去の統合ツール実行（同じ業務方向・同じ文字列）
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', :k, '7500001', 'ASIN_LIST')"),
                     {"k": key.upper()})
    out, led = tmp_path / "o", tmp_path / "o" / "l.jsonl"
    (it,), _ = build_items(app_role_factory, "US", run_id, tool_country="jp", out_dir=out)
    assert it.prior_tool_runs == ["7500001"]
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--tool-country", "jp", "--out-dir", str(out), "--ledger", str(led)]) == 0
    (k,) = SubmissionLedger(led).all_states()
    row = ('<tr data-seq="{s}" data-type="asin_list" data-text="asin" data-country="jp"><td class="log-status">取得完了</td>'
           '<td class="log-csv">-</td><td><textarea class="memo">{m}</textarea></td></tr>')
    html = tmp_path / "log.html"
    html.write_text("<table>" + row.format(s=7500001, m=it.memo) + "</table>", encoding="utf-8")
    assert cli_main(["reconcile", "--key", k, "--log-html", str(html), "--ledger", str(led)]) == 3  # 過去の実行だけ: 確定しない
    assert SubmissionLedger(led).state(k).state == "PLANNED"
    html.write_text("<table>" + row.format(s=7500001, m=it.memo) + row.format(s=7500009, m=it.memo) + "</table>", encoding="utf-8")
    assert cli_main(["reconcile", "--key", k, "--log-html", str(html), "--ledger", str(led)]) == 0
    assert SubmissionLedger(led).state(k).tool_run_id == "7500009"  # 過去の実行を除いた新しい行だけを採用する
