"""取得runから統合ツール用の入力ファイルと投入計画を作る処理・手動手順のCLIの結合テスト（専用test-db。統合ツールには接続しない）。

DBは読み取りだけ（`link-check`も読み取り）。台帳はファイル。ASINリスト入力に国指定は無いので国設定は使わない。"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from acquisition.registry import SourceRegistry
from asin_collector.source_runner import PageLoadError, RunLimits, run_claimed_source
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


def fetched(app_role_factory, registry, runs, tmp_path, market, pages, *, origin_asin, units, name, key=None, limits=None):
    """起点つきの出典を作り、架空の取得を完了させて (出典, run_id, 結果, import_id, キー) を返す。"""
    rid = make_active_report(app_role_factory, tmp_path, market, [(origin_asin, 50, 3.0, units)], name=name)
    key = key or unique_key("ASUB")
    rp = registry.register_path(market, "SELLER", key, origin_import_id=rid, origin_asin=origin_asin)
    claim = runs.claim(market, rp.source.source_id)
    rep = run_claimed_source(runs, app_role_factory, FakeFetcher(*pages), claim, sleep=lambda s: None, limits=limits)
    return rp.source, claim.run_id, rep, rid, key


def _source_id(factory, market, key):
    with factory.get_session(market) as conn, conn.begin():
        return conn.execute(text("SELECT source_id FROM acquisition_source WHERE key_primary = :k"), {"k": key.upper()}).scalar_one()


def test_export_source_gets_an_export_input_without_any_country_setting(app_role_factory, registry, runs, tmp_path):
    src, run_id, rep, rid, key = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(1), asin(2)], "https://www.amazon.com/s?p=2"), results([asin(3)])],
                                         origin_asin=asin(100), units=9, name="a.csv")
    assert rep.outcome == "SUCCESS_NONEMPTY"
    items, files = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out")
    (it,) = items
    assert (it.direction, it.business_market, it.fetch_market, it.source_id, it.collector_run_id) == ("EXPORT", "US", "US", src.source_id, run_id)
    assert it.memo == f"輸出_{key.upper()}" and it.status == "READY"  # 国設定が無くてもDRAFTにならない
    assert not hasattr(it, "tool_country")
    assert files[0] == f"{asin(1)}\n{asin(2)}\n{asin(3)}\n".encode() and (it.asin_count, it.chunk_index, it.chunk_count, it.run_asin_total) == (3, 1, 1, 3)
    assert it.input_sha256 == hashlib.sha256(files[0]).hexdigest()
    assert it.origin == {"market": "US", "import_id": rid, "asin": asin(100), "units": 9}


def test_truncated_run_is_marked_partial_and_unusable_runs_are_refused(app_role_factory, registry, runs, tmp_path):
    _, run_id, rep, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "JP", [results([asin(11)], "https://www.amazon.co.jp/s?p=2")],
                                   origin_asin=asin(101), units=2, name="b.csv", limits=RunLimits(max_pages=1))
    assert rep.outcome == "SUCCESS_TRUNCATED"
    (it,), _ = build_items(app_role_factory, "JP", run_id, out_dir=tmp_path / "out")
    assert (it.direction, it.business_market, it.memo.startswith("輸入_")) == ("IMPORT", "JP", True)
    assert it.input_is_partial is True and "一部のASIN" in it.partial_reason and it.run_outcome == "SUCCESS_TRUNCATED"  # 部分取得からの入力を明示
    rid2 = make_active_report(app_role_factory, tmp_path, "JP", [(asin(102), 5, 1.0, 1)], name="c.csv")
    rp = registry.register_path("JP", "SELLER", unique_key("APART"), origin_import_id=rid2, origin_asin=asin(102))
    claim = runs.claim("JP", rp.source.source_id)
    bad = run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(13)], "https://www.amazon.co.jp/s?p=2"), PageLoadError("x")), claim, sleep=lambda s: None)
    assert bad.outcome == "PARTIAL"
    with pytest.raises(SubmissionError, match="入力に使えません"):
        build_items(app_role_factory, "JP", claim.run_id, out_dir=tmp_path / "out")
    with pytest.raises(SubmissionError, match="存在しません"):
        build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out")


def test_inputs_over_5000_are_split_in_saved_order_with_separate_hashes_and_keys(app_role_factory, registry, runs, tmp_path):
    asins = [f"B1{i:08d}" for i in range(5003)]
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results(asins)], origin_asin=asin(120), units=1, name="big.csv",
                                 limits=RunLimits(max_pages=1))
    items, files = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out")
    assert [(i.chunk_index, i.chunk_count, i.asin_count) for i in items] == [(1, 2, 5000), (2, 2, 3)]
    assert items[0].key != items[1].key and items[0].input_sha256 != items[1].input_sha256 and items[0].input_file != items[1].input_file
    assert all(i.run_asin_total == 5003 and i.direction == "EXPORT" and i.memo == items[0].memo for i in items)
    joined = b"".join(files).decode().split()
    assert joined == asins  # 欠落・重複なし。保存順
    assert [f.count(b"\n") for f in files] == [5000, 3]


def test_direction_comes_from_the_discovery_path_not_from_the_fetch_market(app_role_factory, registry, runs, tmp_path):
    us_rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(103), 80, 4.0, 7)], name="d.csv")
    via = registry.register_path("US", "SELLER", unique_key("AVIA"), origin_import_id=us_rid, origin_asin=asin(103))
    claim0 = runs.claim("US", via.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(14)])), claim0, sleep=lambda s: None)
    key = unique_key("AXJP")
    registry.register_via_path("JP", "SELLER", key, via_market="US", via_source_id=via.source.source_id, via_asin=asin(14), allowed_origin_import_ids=[us_rid])
    claim = runs.claim("JP", _source_id(app_role_factory, "JP", key))
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(15), asin(16)])), claim, sleep=lambda s: None)
    (it,), _ = build_items(app_role_factory, "JP", claim.run_id, out_dir=tmp_path / "out")
    assert (it.fetch_market, it.direction, it.business_market, it.memo.startswith("輸出_")) == ("JP", "EXPORT", "US", True)
    assert [(p["stage"], p["direction"], p["via_source_id"], p["via_asin"], p["origin_import_id"], p["origin_units"]) for p in it.paths] == [
        ("VIA_SOURCE", "EXPORT", via.source.source_id, asin(14), us_rid, 7)]


def test_one_run_used_in_two_directions_makes_two_separate_inputs_with_distinct_keys(app_role_factory, registry, runs, tmp_path):
    us_rid = make_active_report(app_role_factory, tmp_path, "US", [(asin(104), 80, 4.0, 7)], name="e1.csv")
    jp_rid = make_active_report(app_role_factory, tmp_path, "JP", [(asin(105), 80, 4.0, 3)], name="e2.csv")
    key = unique_key("ABOTH")
    registry.register_path("US", "SELLER", key, origin_import_id=us_rid, origin_asin=asin(104))
    rp = registry.register_path("US", "SELLER", key, origin_import_id=jp_rid, origin_asin=asin(105))
    claim = runs.claim("US", rp.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(21), asin(22)])), claim, sleep=lambda s: None)
    items, files = build_items(app_role_factory, "US", claim.run_id, out_dir=tmp_path / "out")
    ex, im = sorted(items, key=lambda i: i.direction)
    assert (ex.direction, im.direction) == ("EXPORT", "IMPORT") and ex.key != im.key and ex.input_file != im.input_file
    assert ex.memo.startswith("輸出_") and im.memo.startswith("輸入_") and ex.other_directions == ["IMPORT"]
    assert (ex.origin["units"], im.origin["units"]) == (7, 3) and files[0] == files[1]


def test_source_without_a_direction_needs_an_explicit_direction_that_matches_the_paths(app_role_factory, registry, runs, tmp_path):
    rp = registry.register_path("US", "SELLER", unique_key("AMAN"))  # 手動登録（起点なし）
    claim = runs.claim("US", rp.source.source_id)
    run_claimed_source(runs, app_role_factory, FakeFetcher(results([asin(31)])), claim, sleep=lambda s: None)
    with pytest.raises(SubmissionError, match="--direction"):
        build_items(app_role_factory, "US", claim.run_id, out_dir=tmp_path / "out")
    (it,), _ = build_items(app_role_factory, "US", claim.run_id, out_dir=tmp_path / "out", direction_override="IMPORT")
    assert (it.direction, it.origin) == ("IMPORT", None)
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(32)])], origin_asin=asin(106), units=1, name="f.csv")
    with pytest.raises(SubmissionError, match="経路の方向"):
        build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out", direction_override="IMPORT")


def test_duplicate_rows_are_removed_in_the_file_but_the_evidence_counts_are_kept(app_role_factory, registry, runs, tmp_path):
    _, run_id, _, _, _ = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(41), asin(42)])], origin_asin=asin(107), units=1, name="g.csv")
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("INSERT INTO collector_asin (marketplace, collector_run_id, asin) VALUES ('US', :r, :a)"), {"r": run_id, "a": asin(41).lower()})
    (it,), (content,) = build_items(app_role_factory, "US", run_id, out_dir=tmp_path / "out")
    assert content == f"{asin(41)}\n{asin(42)}\n".encode()
    assert (it.total_rows, it.asin_count, it.duplicate_rows_removed) == (3, 2, 1)


def test_write_plan_is_idempotent_keeps_existing_state_and_never_overwrites_files(app_role_factory, registry, runs, tmp_path):
    _, run_id, _, _, key_name = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(51), asin(52)])], origin_asin=asin(108), units=4, name="h.csv")
    out = tmp_path / "out"
    ledger = SubmissionLedger(out / "ledger.jsonl")
    items, files = build_items(app_role_factory, "US", run_id, out_dir=out)
    assert [h for _, h in write_plan(items, files, ledger)] == ["新規"]
    items2, files2 = build_items(app_role_factory, "US", run_id, out_dir=out)
    assert [h for _, h in write_plan(items2, files2, ledger)] == ["変更なし"] and len(ledger.events()) == 1  # 再実行で二重記録しない
    key = items[0].key
    with app_role_factory.get_session("US") as conn, conn.begin():  # 計画に関わる情報（既存の実行）が変わると、送信前なら計画を更新する
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', :k, '7600001', 'ASIN_LIST')"), {"k": key_name.upper()})
    items3, _ = build_items(app_role_factory, "US", run_id, out_dir=out)
    assert items3[0].prior_tool_runs == ["7600001"] and [h for _, h in write_plan(items3, files, ledger)] == ["更新"]
    ledger.append(key, "SUBMIT_STARTED", before_run_ids=[], attempt=1)
    items4, _ = build_items(app_role_factory, "US", run_id, out_dir=out)
    how = write_plan(items4, files, ledger)[0][1]
    assert "SUBMITTING" in how  # 送信が始まった後は計画を書き換えない
    (out / f"run{run_id}_EXPORT_{key}.csv").write_bytes(b"tampered\n")
    with pytest.raises(SubmissionError, match="上書きしません"):
        write_plan(items, files, ledger)


LOG_ROW = ('<tr data-seq="{s}" data-type="{t}" data-text="asin" data-country="{c}"><td class="log-status">{st}</td>'
           '<td class="log-csv">-</td><td><textarea class="memo">{m}</textarea></td></tr>')


def _log(tmp_path, name, *rows):
    p = tmp_path / name
    p.write_text("<table>" + "".join(LOG_ROW.format(s=s, t=t, c=c, st="待機中", m=m) for s, t, c, m in rows) + "</table>", encoding="utf-8")
    return str(p)


def _json_file(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_manual_flow_baseline_reconcile_memo_resolve_link_and_output_verification(app_role_factory, registry, runs, tmp_path, monkeypatch, capsys):
    from db.session import SessionFactory

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    _, run_id, _, _, key = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(61), asin(62)])], origin_asin=asin(109), units=2, name="i.csv")
    # 過去の統合ツール実行（同じメモ・同じ種別・メモ保存済み）が既にある
    with app_role_factory.get_session("US") as conn, conn.begin():
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', :k, '7700001', 'ASIN_LIST')"), {"k": key.upper()})
    out, led = tmp_path / "o", tmp_path / "o" / "l.jsonl"
    base = ["--ledger", str(led)]
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--out-dir", str(out), *base]) == 0
    assert "国設定" not in capsys.readouterr().out
    ledger = SubmissionLedger(led)
    (k,) = ledger.all_states()
    memo = f"輸出_{key.upper()}"
    before = _log(tmp_path, "before.html", (7700001, "asin_list", "jp", memo))
    opts = _json_file(tmp_path / "opts.json", {"CNフラグ除外": True, "バリエーション": False})
    after1 = _log(tmp_path, "after1.html", (7700001, "asin_list", "jp", memo), (7700002, "asin_list", "jp", ""))
    assert cli_main(["reconcile", "--key", k, "--log-html", after1, *base]) == 2  # 送信前の基準が無いと、過去の行と取り違えるので特定しない
    assert cli_main(["begin", "--key", k, "--log-json", before, *base]) == 2  # 画面の設定値を読めていないと送信権を与えない
    assert cli_main(["begin", "--key", k, "--log-json", before, "--options-json", opts, *base]) == 0
    assert ledger.state(k).state == "SUBMITTING"
    assert cli_main(["begin", "--key", k, "--log-json", before, "--options-json", opts, *base]) == 2  # 二重に送信権は与えない
    asins = (out / f"run{run_id}_EXPORT_{k}.csv").read_text(encoding="utf-8").split()
    req_opts = {"variation": "", "sellers": "on"}
    cap = {"endpoint": "./out/start_search4.php", "body": json.dumps({"url": ",".join(asins), "options": req_opts, "type": "asin_list", "country": ""}),
           "status": 200, "response": "start_search4asin_listrivalsuccess asin1 https://example.invalid/csv_del.php<br>"}
    assert cli_main(["reconcile", "--key", k, "--log-html", after1, *base]) == 3  # 根拠が無いと、新規行が1件でも自分の送信と断定しない
    assert ledger.state(k).state == "UNKNOWN"
    # 結果不明のまま未送信へは戻さない。根拠つきの別の試行で、特定・メモ保存・対応記録の流れを確認する
    out2, led2 = tmp_path / "o2", tmp_path / "o2" / "l.jsonl"
    base2 = ["--ledger", str(led2)]
    assert cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--out-dir", str(out2), *base2]) == 0
    ledger = SubmissionLedger(led2)
    (k,) = ledger.all_states()
    assert cli_main(["begin", "--key", k, "--log-json", before, "--options-json", opts, *base2]) == 0
    assert cli_main(["evidence", "--key", k, "--capture-json", _json_file(tmp_path / "cap.json", cap), *base2]) == 0
    options_row = "{cn_safe:on,variation:,sellers:on}"
    after1 = _json_file(tmp_path / "after1.json", [{"seq": 7700001, "data_type": "asin_list", "memo": memo}, {"seq": 7700002, "data_type": "asin_list", "memo": "", "options": options_row}])
    base = base2
    assert cli_main(["reconcile", "--key", k, "--log-html", after1, *base]) == 3  # 行の設定値が送信リクエストの設定値と一致しないので特定しない
    ledger.append(k, "SUBMIT_STARTED", before_run_ids=[7700001], attempt=2, options={"バリエーション": False})  # 再試行（根拠は取り直し）
    cap2 = dict(cap, body=json.dumps({"url": ",".join(asins), "options": {"cn_safe": "on", "variation": "", "sellers": "on"}, "type": "asin_list", "country": ""}))
    assert cli_main(["evidence", "--key", k, "--capture-json", _json_file(tmp_path / "cap2.json", cap2), *base]) == 0
    assert cli_main(["reconcile", "--key", k, "--log-html", after1, *base]) == 0
    st = ledger.state(k)
    assert (st.state, st.tool_run_id) == ("RUN_IDENTIFIED", "7700002")  # 過去の7700001（同じメモ）ではなく、基準に無い新しい行
    assert cli_main(["reconcile", "--key", k, "--log-html", after1, *base]) == 3  # メモ未保存: ASINは再投入せずメモ保存を促す
    assert "再投入しません" in capsys.readouterr().out
    after2 = _log(tmp_path, "after2.html", (7700001, "asin_list", "jp", memo), (7700002, "asin_list", "jp", memo))
    assert cli_main(["memo-start", "--key", k, *base]) == 0
    assert cli_main(["reconcile", "--key", k, "--log-html", after2, *base]) == 0
    assert ledger.state(k).state == "MEMO_SAVED"
    assert cli_main(["link-check", "--key", k, *base]) == 3  # まだingest-logされていない
    with app_role_factory.get_session("US") as conn, conn.begin():  # 既存のingest-logが記録した状態を再現
        conn.execute(text("INSERT INTO tool_execution_link (marketplace, seller_id, tool_run_id, source) VALUES ('US', :k, '7700002', 'ASIN_LIST')"), {"k": key.upper()})
    assert cli_main(["link-check", "--key", k, *base]) == 0 and ledger.state(k).state == "LINKED"
    good = tmp_path / "out.csv"
    good.write_text("ASIN,x\n" + "\n".join(f"{a},1" for a in asins) + "\n", encoding="utf-8")
    asins = (out2 / f"run{run_id}_EXPORT_{k}.csv").read_text(encoding="utf-8").split()
    good.write_text("ASIN,x\n" + "\n".join(f"{a},1" for a in asins) + "\n", encoding="utf-8")
    assert cli_main(["verify-output", "--key", k, "--csv", str(good), *base]) == 0
    good.write_text("ASIN,x\nB0OTHER000,1\n", encoding="utf-8")
    assert cli_main(["verify-output", "--key", k, "--csv", str(good), *base]) == 3  # 別の実行の出力かもしれない


def test_manual_flow_ambiguity_stops_and_only_a_human_choice_continues(app_role_factory, registry, runs, tmp_path, monkeypatch, capsys):
    from db.session import SessionFactory

    monkeypatch.setattr(SessionFactory, "for_app_role", classmethod(lambda cls: app_role_factory))
    _, run_id, _, _, key = fetched(app_role_factory, registry, runs, tmp_path, "US", [results([asin(71)])], origin_asin=asin(110), units=2, name="j.csv")
    out, led = tmp_path / "o", tmp_path / "o" / "l.jsonl"
    base = ["--ledger", str(led)]
    cli_main(["plan", "--fetch-market", "US", "--run-id", str(run_id), "--out-dir", str(out), *base])
    ledger = SubmissionLedger(led)
    (k,) = ledger.all_states()
    opts = _json_file(tmp_path / "opts.json", {"バリエーション": False})
    assert cli_main(["begin", "--key", k, "--log-json", _log(tmp_path, "b.html", (7800001, "asin_list", "jp", "")), "--options-json", opts, *base]) == 0
    two = _log(tmp_path, "two.html", (7800001, "asin_list", "jp", ""), (7800002, "asin_list", "jp", ""), (7800003, "asin_list", "us", ""))
    assert cli_main(["reconcile", "--key", k, "--log-html", two, "--no-evidence", *base]) == 3 and ledger.state(k).state == "AMBIGUOUS"
    assert "メモを付けないでください" in capsys.readouterr().out
    assert cli_main(["resolve", "--key", k, "--run-id", "7800001", *base]) == 2  # 候補外（基準にある行）は選べない
    assert cli_main(["resolve", "--key", k, "--run-id", "7800003", *base]) == 0
    assert (ledger.state(k).state, ledger.state(k).tool_run_id) == ("RUN_IDENTIFIED", "7800003")
