"""第3報の追加分（docs/DEVIATIONS.md #98）の単体テスト。DB・ブラウザ・実データ不要（合成データ・一時ディレクトリのみ）。
低効率の窓判定・選択可能な出典数・取得対象の前後比較・取り直し見送りの影響・候補不足の解除・D分類の説明と取り分け・読み取り専用の接続解決。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_submission.ledger import SubmissionLedger
from workflow import progress as P
from workflow.config import Completion, ConfigError, config_from_dict
from workflow.quarantine import FILE_NAME, NeedsHuman, REVIEW_DAYS, format_items
from workflow.reconcile import apply_reconcile, explain_repeat, format_scan, reopen, scan
from workflow.restart_sim import replay, simulate
from workflow.source_select import baseline, choose, compare, format_compare, selectable_counts
from workflow.store import State, Task, WorkflowStore

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
CFG = Completion()


def prog(tmp_path, now=NOW):
    return P.SourceProgress(tmp_path / "source_progress.jsonl", now=lambda: now)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- 設定（しきい値は設定に集約・後方互換）

def test_completion_config_defaults_roundtrip_validation_and_old_config_compat():
    base = {"lanes": {"US": {"origin_import": 1}}}
    cfg = config_from_dict(base)                                                       # 古い設定（completion無し）はそのまま読める
    assert cfg.completion == Completion() and cfg.needed_sources() >= 1
    again = config_from_dict(cfg.to_dict())
    assert again.completion == cfg.completion and again.sha256() == cfg.sha256()
    assert config_from_dict({**base, "completion": {"min_selectable": 7}}).needed_sources() == 7
    assert config_from_dict({**base, "completion": {"_comment": "x", "backoff_max_days": 14}}).completion.backoff_max_days == 14
    for bad in ({"low_yield_new_rate": 1.5}, {"backoff_start_days": 5, "backoff_max_days": 2}, {"unknown_key": 1}, {"low_yield_window_runs": 0}):
        with pytest.raises(ConfigError):
            config_from_dict({**base, "completion": bad})
    old_state = State("wf-x", "t", {"lanes": {"US": {"origin_import": 1}}}, "h")        # 状態ファイルの保存済み設定にcompletionが無くても読める
    assert config_from_dict(old_state.config).completion == Completion()
    assert "completion" not in json.loads(old_state.to_json())                          # 状態ファイルの最上位に項目を足さない


# ---------------------------------------------------------------- 低効率の窓判定（短く始め、連続したときだけ延ばし、回復は即時）

def sample(p, sid, run_id, pages, total, new, **kw):
    return P.note_low_yield(p, "US", sid, wf="w", run_id=run_id, pages=pages, total=total, new=new, cfg=kw.get("cfg"))


def test_low_yield_starts_short_extends_only_on_consecutive_lows_and_is_capped(tmp_path):
    now = Clock(NOW)
    p = P.SourceProgress(tmp_path / "p.jsonl", now=now)
    days = []
    for i in range(7):
        sample(p, 4, i, 30, 600, 6)                                                    # 初見率1%・30頁（窓の合計が20頁以上）
        days.append((P.parse_iso(p.state("US", 4).low_yield["reevaluate_at"]) - now()).days)
        now.t += timedelta(hours=1)
    assert days[:5] == [1, 2, 4, 8, 14] and days[5:] == [14, 14]                         # 1日から始まり、連続するたびに2倍、14日で頭打ち
    assert P.backoff_days(0, CFG) == 1 and P.backoff_days(99, CFG) == 14


def test_low_yield_uses_a_window_of_recent_runs_not_a_single_run(tmp_path):
    p = prog(tmp_path)
    assert sample(p, 5, 1, 12, 240, 0) is None                                         # 1回12頁だけでは窓の合計(20頁)に満たず判定しない
    assert p.deprioritized("US") == set()
    assert sample(p, 5, 2, 12, 240, 1) is not None and p.deprioritized("US") == {5}      # 直近2回の合算24頁・初見率0.2%で低効率
    j = p.state("US", 5).low_yield["window"]
    assert j["runs"] == 2 and j["pages"] == 24 and j["new"] == 1


def test_a_single_good_run_releases_immediately_and_old_samples_leave_the_window(tmp_path):
    now = Clock(NOW)
    p = P.SourceProgress(tmp_path / "p.jsonl", now=now)
    sample(p, 6, 1, 30, 600, 3)
    assert p.deprioritized("US") == {6}
    now.t += timedelta(hours=2)
    sample(p, 6, 2, 12, 240, 120)                                                      # 回復: そのrun自身の初見率が高ければ、窓の合算に関係なく即解除
    assert p.deprioritized("US") == set() and p.state("US", 6).low_yield is None
    now.t += timedelta(days=4)                                                         # 3日の窓を過ぎた標本は判定に使わない
    sample(p, 6, 3, 30, 600, 2)
    j = p.state("US", 6).low_yield["window"]
    assert j["runs"] == 1 and j["pages"] == 30 and p.state("US", 6).low_yield["level"] == 0


def test_low_yield_thresholds_come_from_the_config(tmp_path):
    strict = Completion(low_yield_new_rate=0.5, low_yield_window_min_pages=10, low_yield_min_pages=10)
    p = prog(tmp_path)
    sample(p, 7, 1, 10, 100, 30, cfg=strict)                                           # 30%は既定なら正常、厳しい設定なら低効率
    assert p.deprioritized("US") == {7}
    p2 = P.SourceProgress(tmp_path / "p2.jsonl", now=lambda: NOW)
    sample(p2, 7, 1, 10, 100, 30)
    assert p2.deprioritized("US") == set()


# ---------------------------------------------------------------- 選択可能な出典数・取得対象の前後比較

def entry(sid, units=10):
    return SimpleNamespace(facts=SimpleNamespace(source_id=sid), rep=SimpleNamespace(units=units) if units is not None else None)


def plan_of(export, imp=()):
    return SimpleNamespace(sections={"EXPORT": [entry(s) for s in export], "IMPORT": [entry(s) for s in imp]})


def test_choose_equals_the_old_selection_when_nothing_is_slow():
    plan = plan_of(range(1, 31), range(31, 40))
    before = [e.facts.source_id for e in baseline(plan)]
    sel = choose(plan, needed=3)
    assert [e.facts.source_id for e in sel.picks] == before and len(before) == 29 and sel.selectable == 39 and not sel.scarce


def test_slow_sources_are_ranked_lower_without_freeing_slots_or_excluding_them():
    plan = plan_of(range(1, 22))                                                        # 21候補・1方向の上限20
    slow = {1: "2026-11-09T00:00:00Z", 2: "2026-11-07T00:00:00Z"}                          # 上位2つが低効率で後退中
    sel = choose(plan, slow=slow, needed=3)
    ids = [e.facts.source_id for e in sel.picks]
    assert len(ids) == 20 == len(baseline(plan))                                         # 枠は減らさない
    assert ids == list(range(3, 22)) + [2]                                               # 通常の出典が先、空いた枠は再評価日が近い後退中の出典が埋める
    assert sel.selectable == 19 and sel.deprioritized == 2 and not sel.scarce
    cmp = compare(plan, slow, set(), needed=3)
    assert cmp["same_count"] and cmp["moved_down"] == [2] and cmp["dropped"] == [1] and cmp["added"] == [21]


def test_slow_sources_beyond_the_limit_drop_out_of_this_round_only_and_never_permanently():
    plan = plan_of(range(1, 26))
    slow = {s: "2026-11-09T00:00:00Z" for s in range(1, 6)}
    ids = [e.facts.source_id for e in choose(plan, slow=slow, needed=3).picks]
    assert ids == list(range(6, 26))[:20] and not set(slow) & set(ids)                     # 通常の出典だけで枠が埋まれば、後退中は今回は選ばれない
    assert [e.facts.source_id for e in choose(plan, slow={}, needed=3).picks] == list(range(1, 21))   # 後退が解除されれば元の順位に戻る


def test_scarce_candidates_bring_slow_sources_back_before_their_reevaluation_date():
    plan = plan_of([1, 2, 3, 4])
    slow = {1: "2026-12-01T00:00:00Z", 2: "2026-12-01T00:00:00Z", 3: "2026-12-01T00:00:00Z"}
    sel = choose(plan, slow=slow, needed=3)                                              # 選択可能1件 < 必要3件
    assert sel.scarce and sel.selectable == 1
    assert [e.facts.source_id for e in sel.picks] == [1, 2, 3, 4]                         # 後退中を戻し、元の順位で並ぶ
    assert sel.per_section["EXPORT"]["released"] == 3
    ok = choose(plan, slow={1: "2026-12-01T00:00:00Z"}, needed=3)                         # 選択可能3件 = 必要3件 → 不足ではない
    assert not ok.scarce and [e.facts.source_id for e in ok.picks] == [2, 3, 4, 1]


def test_unevaluated_sources_keep_reserved_slots():
    plan = plan_of(range(1, 31))
    evaluated = set(range(1, 26))                                                         # 1〜25は評価済み、26〜30が未評価
    sel = choose(plan, evaluated=evaluated, reserve=3, needed=3)
    ids = [e.facts.source_id for e in sel.picks]
    assert len(ids) == 20 and {26, 27, 28} <= set(ids)                                    # 未評価に3枠を確保
    assert sel.per_section["EXPORT"]["reserved_in"] == 3
    assert ids == sorted(ids)                                                             # 並びは元の順位順
    assert len(choose(plan, evaluated=evaluated, reserve=0, needed=3).picks) == 20
    assert not {26, 27, 28} & {e.facts.source_id for e in choose(plan, evaluated=evaluated, reserve=0, needed=3).picks}


def test_selectable_counts_and_the_plan_compare_text_are_pinned():
    plan = plan_of([1, 2, 3, 4, 5], [6, 7])
    slow = {2: "2026-11-09T00:00:00Z"}
    c = selectable_counts(plan, slow, {1, 2, 3}, needed=3, reserve=1)
    assert c["selectable"] == 6 and c["deprioritized"] == 1 and c["scarce"] is False
    assert c["sections"]["EXPORT"] == {"candidates": 5, "selectable": 4, "deprioritized": 1, "unevaluated": 2, "picked": 5, "reserved_in": 0, "released": 0}
    text = "\n".join(format_compare("US", compare(plan, slow, {1, 2, 3}, needed=3, reserve=1), slow))
    assert "選択可能な出典: 6件（後退中1件 / 必要数3件）" in text and "後ろへ回した: [2]" in text and "従来: #1 #2 #3 #4 #5 #6 #7" in text
    assert "新  : #1 #3 #4 #5 #6 #7 #2*" in text.replace("  新  :", "  新  :")


def test_zero_unit_and_no_origin_entries_follow_the_old_scope():
    plan = SimpleNamespace(sections={"EXPORT": [entry(1, units=0), entry(2, units=None), entry(3)], "IMPORT": []})
    assert [e.facts.source_id for e in baseline(plan)] == [1, 3] == [e.facts.source_id for e in choose(plan, needed=1).picks]   # 起点の経路が無い出典は対象外


# ---------------------------------------------------------------- 取り直しの見送り: 候補不足の解除と取得量への影響

BIG = {"prior_pages": 60, "last_error": "REPEATED_PAGE", "restart_streak": 1}


def test_restart_gate_is_released_only_when_valid_alternatives_are_scarce(tmp_path):
    p = prog(tmp_path)
    p.append("US", 3, "ACQUIRING", wf="w", plan="RESTART_BROKEN", pid=1, start_page=1)
    assert P.restart_gate(p, "US", 3, BIG, alternatives=5, needed=3)["kind"] == P.WAIT_RESTART_REVIEW   # 他に候補が十分: 見送る
    assert P.restart_gate(p, "US", 3, BIG, alternatives=2, needed=3) is None                             # 候補不足: 解除して取り直す
    assert P.restart_gate(p, "US", 3, BIG, alternatives=0, needed=3, release_when_scarce=False) is not None   # 解除を無効にした設定
    assert P.restart_gate(p, "US", 3, BIG) is not None                                                   # 引数なしは従来の挙動
    assert P.gate_wait(1) == timedelta(hours=24) and P.gate_wait(2) == timedelta(hours=48) and P.gate_wait(9) == timedelta(hours=24 * 16)


def restart_rows():
    """実測の形（コーディネーター報告）を模した合成データ: US 10 run / 372頁（#784・#78は60頁を2回、ほかは22頁、重複率100%）。"""
    t0 = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
    spec = [(784, 60, 0), (78, 60, 1), (784, 60, 5), (78, 60, 6), (213, 22, 8), (417, 22, 9), (456, 22, 10), (569, 22, 11), (371, 22, 12), (595, 22, 13)]
    return [{"id": i + 1, "source_id": sid, "pages": pg, "prior_pages": 60 if pg == 60 else 25, "rows_": pg * 50, "overlap": pg * 50,
             "started_at": t0 + timedelta(hours=h)} for i, (sid, pg, h) in enumerate(spec)]


def test_restart_simulation_numbers_for_reallocation_scarcity_and_the_worst_case():
    rows = restart_rows()
    assert sum(r["pages"] for r in rows) == 372
    rr = replay(rows)
    assert [r["id"] for r in rr if r["deferred"]] == [3, 4]                                # 同じ出典を5〜6時間後に取り直した2run(60頁ずつ)だけが見送り対象
    ok = simulate(rows, alternatives=3, needed=3, pages_per_source=60, new_per_page=2.0)
    assert (ok["restart_runs"], ok["restart_pages"], ok["deferred_runs"], ok["deferred_pages"]) == (10, 372, 2, 120)
    assert ok["refetch_pages_avoided"] == 120.0 and ok["new_asins_forgone_by_deferral"] == 0       # 見送った取り直しは重なり100%で、失うASINは0
    assert (ok["reallocated_pages"], ok["unused_pages"], ok["released_runs"]) == (120, 0, 0)
    assert ok["reallocated_new_asins_est"] == 240.0 and ok["net_new_asins_est"] == 240.0
    thin = simulate(rows, alternatives=1, needed=1, pages_per_source=60, new_per_page=2.0)          # 他の候補が1つだけ: 60頁だけ再配分され、60頁は使われず残る
    assert (thin["reallocated_pages"], thin["unused_pages"]) == (60, 60)
    scarce = simulate(rows, alternatives=0, needed=3, pages_per_source=60, new_per_page=2.0)        # 候補なし: 見送りを解除（取り直しは行われ影響0）
    assert scarce["scarce"] and scarce["released_runs"] == 2 and scarce["reallocated_pages"] == 0 and scarce["net_new_asins_est"] == 0.0
    assert scarce["worst_case_unused_pages_if_no_release"] == 120


def test_restart_simulation_allows_later_restarts_and_small_cycles():
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = [{"id": 1, "source_id": 9, "pages": 60, "prior_pages": 60, "rows_": 100, "overlap": 100, "started_at": t0},
            {"id": 2, "source_id": 9, "pages": 60, "prior_pages": 60, "rows_": 100, "overlap": 90, "started_at": t0 + timedelta(hours=25)},       # 24時間後 → 許可
            {"id": 3, "source_id": 9, "pages": 60, "prior_pages": 60, "rows_": 100, "overlap": 100, "started_at": t0 + timedelta(hours=26)},      # 2回目の後は48時間待つ → 見送り
            {"id": 4, "source_id": 8, "pages": 5, "prior_pages": 5, "rows_": 10, "overlap": 10, "started_at": t0},
            {"id": 5, "source_id": 8, "pages": 5, "prior_pages": 5, "rows_": 10, "overlap": 10, "started_at": t0 + timedelta(hours=1)}]          # 小さい一巡は見送らない
    assert [r["id"] for r in replay(rows) if r["deferred"]] == [3]


# ---------------------------------------------------------------- D分類: 理由別の内訳・--explain・取り分け

def put_ledger(tmp_path):
    led = SubmissionLedger(tmp_path / "tool_submission" / "ledger.jsonl")
    files = tmp_path / "inputs"
    files.mkdir(parents=True)

    def mk(name, asins):
        f = files / f"{name}.txt"
        f.write_text("\n".join(asins) + "\n", encoding="utf-8")
        return str(f)

    def man(name, asins, repeat=False, run=1):
        return {"asin_count": len(asins), "direction": "EXPORT", "business_market": "US", "repeat_of_earlier_cycle": repeat, "source_id": 7,
                "collector_run_id": run, "input_file": mk(name, asins)}

    a = [f"B0A{i:07d}" for i in range(10)]
    led.append("reg1", "PLANNED", manifest=man("reg1", a[:6], run=11))
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "801"}), ("MEMO_SAVED", {}), ("LINKED", {})):
        led.append("reg1", ev, **kw)
    led.append("reg2", "PLANNED", manifest=man("reg2", a[6:8], run=12))
    for ev, kw in (("SUBMIT_STARTED", {"before_run_ids": []}), ("RUN_IDENTIFIED", {"tool_run_id": "802"}), ("MEMO_SAVED", {}), ("LINKED", {})):
        led.append("reg2", ev, **kw)
    led.append("rep_partial", "PLANNED", manifest=man("rep_partial", a, repeat=True, run=21))        # 10件のうち8件が登録済み(6+2)、2件が未登録
    led.append("rep_all", "PLANNED", manifest=man("rep_all", a[:4], repeat=True, run=22))              # 全て登録済み
    led.append("rep_none", "PLANNED", manifest=man("rep_none", ["B0Z0000001", "B0Z0000002"], repeat=True, run=23))
    led.append("rep_missing", "PLANNED", manifest={**man("rep_missing", ["B0Y0000001"], repeat=True, run=24), "input_file": str(files / "gone.txt")})
    return led


def test_explain_repeat_reports_overlap_with_registered_runs_and_remaining_unregistered(tmp_path):
    led = put_ledger(tmp_path)
    st = led.all_states()
    x = explain_repeat("rep_partial", st)
    assert (x["ledger_state"], x["in_file"], x["overlap_with_registered"], x["unregistered_remaining"]) == ("PLANNED", 10, 8, 2)
    assert [(r["tool_run_id"], r["overlap"]) for r in x["registered_runs"]] == [("801", 6), ("802", 2)] and x["verdict"].startswith("PARTIAL")
    assert explain_repeat("rep_all", st)["verdict"].startswith("ALL_ALREADY_REGISTERED") and explain_repeat("rep_all", st)["unregistered_remaining"] == 0
    assert explain_repeat("rep_none", st)["verdict"].startswith("NONE_REGISTERED")
    m = explain_repeat("rep_missing", st)
    assert m["verdict"].startswith("INPUT_FILE_MISSING") and m["in_file"] is None and m["unregistered_remaining"] is None


def repeat_workflows(tmp_path, keys=("rep_partial", "rep_all", "rep_none", "rep_missing", "ghost")):
    store = WorkflowStore(tmp_path / "workflow")
    for i, k in enumerate(keys):
        for j in range(2 if k == "rep_partial" else 1):                                  # rep_partial は2つのworkflowに現れる
            st = State(f"wf-20261105-{i}{j}0000-aaaa", "2026-11-05T00:00:00Z", {}, "h")
            st.tasks[f"submit:{k}"] = Task(f"submit:{k}", "submit", "US", status="HELD", detail="過去の一巡で投入済み", result={"key": k, "asin_count": 5, "repeat": True})
            store.save(st)
    return store


def tree(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_reconcile_dry_run_breaks_d_down_by_reason_and_explains_each_repeat_key_without_writing(tmp_path):
    led = put_ledger(tmp_path)
    store = repeat_workflows(tmp_path)
    before = tree(tmp_path)
    rep = scan(store, led, explain=True)
    assert tree(tmp_path) == before                                                    # 何も書かない
    assert rep["d_breakdown"] == {"NO_LEDGER_KEY": {"unique_keys": 1, "workflows": 1, "quarantinable": True},
                                  "REPEAT_OF_EARLIER_CYCLE": {"unique_keys": 4, "workflows": 5, "quarantinable": True}}
    assert sorted(x["key"] for x in rep["explain_repeat"]) == ["rep_all", "rep_missing", "rep_none", "rep_partial"]
    text = "\n".join(format_scan(rep))
    assert "REPEAT_OF_EARLIER_CYCLE: キー4件 / workflow5件" in text and "登録済みと重なる8件 / 未登録のまま2件" in text and "実行ID=801" in text
    assert "explain_repeat" not in "\n".join(format_scan(scan(store, led)))              # --explainなしでは各キーの説明は出さない
    assert scan(store, led)["explain_repeat"] == []


def test_apply_include_d_quarantines_into_needs_human_without_touching_the_ledger_and_is_idempotent(tmp_path):
    led = put_ledger(tmp_path)
    store = repeat_workflows(tmp_path)
    nh = NeedsHuman(tmp_path / "workflow" / FILE_NAME, now=lambda: NOW)
    led_bytes = led.path.read_bytes()
    prog_ = P.SourceProgress(tmp_path / "workflow" / "source_progress.jsonl", now=lambda: NOW)
    plain = apply_reconcile(store, led, prog_, now=NOW, confirm=lambda q: True)                  # --include-dなし: Dは触らない
    assert plain["changed"] == [] and nh.open_items() == []
    out = apply_reconcile(store, led, prog_, now=NOW, confirm=lambda q: True, include_d=True, needs=nh)
    assert len(out["changed"]) == 6 and led.path.read_bytes() == led_bytes                       # 6 workflow（rep_partialは2つ）を整理。台帳は不変
    items = nh.open_items()
    assert sorted(i.key for i in items) == ["ghost", "rep_all", "rep_missing", "rep_none", "rep_partial"]
    part = next(i for i in items if i.key == "rep_partial")
    assert len(part.workflows) == 2 and part.info["remaining"] == 2 and part.review_due == (NOW + timedelta(days=REVIEW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = store.load(part.workflows[0]["workflow"])
    t = st.tasks["submit:rep_partial"]
    assert t.status == "DONE" and t.result["closed_reason"] == "NEEDS_HUMAN" and "登録済みではありません" in t.detail and t.result["closed_from"]["status"] == "HELD"
    again = apply_reconcile(store, led, prog_, now=NOW, confirm=lambda q: True, include_d=True, needs=nh)
    assert again["changed"] == [] and len(nh.open_items()) == 5                                  # 冪等
    assert len(list(store.dir(part.workflows[0]["workflow"]).glob("state.json.reconcile-*.bak"))) == 1   # 元の状態は退避される


def test_needs_human_overdue_resolve_and_reopen_with_allow_duplicate(tmp_path):
    led = put_ledger(tmp_path)
    store = repeat_workflows(tmp_path)
    clock = Clock(NOW)
    nh = NeedsHuman(tmp_path / "workflow" / FILE_NAME, now=clock)
    apply_reconcile(store, led, P.SourceProgress(tmp_path / "workflow" / "source_progress.jsonl", now=clock), now=NOW, confirm=lambda q: True, include_d=True, needs=nh)
    assert nh.overdue() == []
    clock.t = NOW + timedelta(days=REVIEW_DAYS, minutes=1)
    assert len(nh.overdue()) == 5 and "期限切れ5件" in "\n".join(format_items(nh.open_items(), clock.t))      # 期限を過ぎたら期限切れとして出続ける
    items = {i.key: i for i in nh.open_items()}
    nh.resolve(items["rep_all"].id, "ALREADY_REGISTERED", "登録済みと確認")
    assert len(nh.open_items()) == 4 and nh.items()[items["rep_all"].id].resolved["decision"] == "ALREADY_REGISTERED"
    with pytest.raises(KeyError):
        nh.resolve(items["rep_all"].id, "OTHER", "")                                          # 解決済みは二重に解決できない
    ch = reopen(store, nh, items["rep_none"].id, "意図した再投入", allow_duplicate=True)
    assert len(ch) == 1 and ch[0]["status"] == "HELD"
    st = store.load(ch[0]["workflow"])
    assert st.tasks["submit:rep_none"].status == "HELD" and "closed_reason" not in st.tasks["submit:rep_none"].result and "rep_none" in st.allow_duplicates
    assert items["rep_none"].id not in {i.id for i in nh.open_items()}
    assert [e["event"] for e in nh.events()].count("NOTED") == 5                              # 追記専用（過去の行は書き換えない）


def test_unresolvable_states_are_never_quarantined(tmp_path):
    led = SubmissionLedger(tmp_path / "tool_submission" / "ledger.jsonl")
    led.append("unk", "PLANNED", manifest={"asin_count": 3, "direction": "EXPORT", "business_market": "US", "repeat_of_earlier_cycle": False})
    led.append("unk", "SUBMIT_STARTED", before_run_ids=[])
    led.append("unk", "OUTCOME_UNKNOWN")
    store = WorkflowStore(tmp_path / "workflow")
    st = State("wf-u", "t", {}, "h")
    st.tasks["submit:unk"] = Task("submit:unk", "submit", "US", status="UNKNOWN", result={"key": "unk"})
    store.save(st)
    nh = NeedsHuman(tmp_path / "workflow" / FILE_NAME, now=lambda: NOW)
    rep = scan(store, led)
    assert rep["d_breakdown"]["LEDGER_UNKNOWN"]["quarantinable"] is False
    out = apply_reconcile(store, led, P.SourceProgress(tmp_path / "workflow" / "p.jsonl", now=lambda: NOW), now=NOW, confirm=lambda q: True, include_d=True, needs=nh)
    assert out["changed"] == [] and nh.open_items() == [] and store.load("wf-u").tasks["submit:unk"].status == "UNKNOWN"   # 結果不明は未完了のまま見える


def test_watch_notifies_overdue_needs_human_but_not_before_the_deadline(tmp_path):
    from workflow.watch import Watcher

    clock = Clock(NOW)
    nh = NeedsHuman(tmp_path / FILE_NAME, now=clock)
    nh.note("SUBMIT_KEY", "k1", "REPEAT_OF_EARLIER_CYCLE", [{"workflow": "w", "task": "submit:k1", "from_status": "HELD"}], {"summary": "x"})
    seen = []
    fake = SimpleNamespace(cfg=SimpleNamespace(state_dir=str(tmp_path)), now=clock, _notify_stall=lambda key, msg, market, res: seen.append((key, msg)))
    Watcher._check_needs_human(fake, SimpleNamespace())
    assert seen == []
    clock.t = NOW + timedelta(days=REVIEW_DAYS + 1)
    Watcher._check_needs_human(fake, SimpleNamespace())
    assert len(seen) == 1 and "期限切れ" in seen[0][1] and "1件" in seen[0][1]


# ---------------------------------------------------------------- CLI・読み取り専用の接続解決

def test_reconcile_and_needs_human_cli(tmp_path, capsys):
    from workflow.cli import main

    led = put_ledger(tmp_path)
    repeat_workflows(tmp_path)
    args = ["--state-dir", str(tmp_path / "workflow"), "--submission-dir", str(tmp_path / "tool_submission")]
    before = tree(tmp_path)
    assert main(["reconcile", "--explain", *args]) == 0
    out = capsys.readouterr().out
    assert tree(tmp_path) == before and "[REPEAT]" in out and "未登録のまま2件" in out and "dry-run" in out
    assert main(["reconcile", "--apply", "--include-d", *args], confirm=lambda q: True) == 0
    capsys.readouterr()
    assert main(["needs-human", "--state-dir", str(tmp_path / "workflow")]) == 0
    listing = capsys.readouterr().out
    assert "人の判断待ち(needs-human): 5件" in listing and "REPEAT_OF_EARLIER_CYCLE" in listing
    nid = listing.split("nh-", 1)[1][:10]
    assert main(["needs-human", "--state-dir", str(tmp_path / "workflow"), "--resolve", "nh-" + nid, "--decision", "WITHDRAWN", "--note", "x"]) == 0
    assert main(["needs-human", "--state-dir", str(tmp_path / "workflow"), "--resolve", "nh-" + nid]) == 2        # 解決済みの再解決は中止
    assert led.path.exists()


def test_readonly_connection_resolves_itself_without_env_vars(monkeypatch):
    from workflow import dbconn

    monkeypatch.delenv("SEEDASIN_APP_DATABASE_URL", raising=False)
    url, origin = dbconn.resolve_app_url()
    assert url.startswith("postgresql+psycopg://app_role:local_dev_app_role_pw@localhost:5432/seedasin") and "local-dev" in origin   # 環境変数なしでも決まる
    assert "admin_role" not in url and "superuser" not in url                              # 使うのはapp_roleだけ
    monkeypatch.setenv("SEEDASIN_LOCAL_DB_PORT", "55511")
    assert ":55511/" in dbconn.resolve_app_url()[0]
    monkeypatch.setenv("SEEDASIN_APP_DATABASE_URL", "postgresql+psycopg://app_role:x@h:1/d")
    assert dbconn.resolve_app_url() == ("postgresql+psycopg://app_role:x@h:1/d", "env:SEEDASIN_APP_DATABASE_URL")
    monkeypatch.delenv("SEEDASIN_APP_DATABASE_URL")
    assert type(dbconn.readonly_factory()).__name__ == "SessionFactory"                      # エンジンは遅延接続: ここでは接続しない


def test_format_outcome_shows_elapsed_seconds_and_the_restart_simulation():
    from workflow.outcome import format_outcome

    sim = simulate(restart_rows(), alternatives=3, needed=3, pages_per_source=60, new_per_page=2.0)
    m = {"market": "US", "elapsed_sec": 1.25, "runs": 3, "pages": 100, "rows": 900, "unique": 800, "duplicates": 100, "new_asins": 200, "new_rate": 0.25,
         "page_cap_runs": 1, "failed_runs": 0, "interrupted_runs": 0, "outcomes": [], "running": [], "restart_refetch": [],
         "restart_measured": {"runs": 0, "pages": 0, "rows": 0, "overlap": 0, "refetched_pages": 0}, "estimated_wasted_pages": 75.0, "sources": [],
         "sources_total": 0, "restart_gate_simulation": [{"case": "既知", **sim}], "selectable": 12}
    o = {"since": "2026-11-04T00:00:00+00:00", "until": "2026-11-05T00:00:00+00:00", "elapsed_sec": 2.5, "markets": {"US": m},
         "ledger": {"started_in_window": {"EXPORT": {"keys": 2, "asins": 50}}, "started_by_state": {"LINKED": 2}, "unregistered_now": {}},
         "carryover": {"carried_in_window": {}, "open_now": {}, "low_yield_now": 1, "deprioritized_now": {"US": 1}},
         "workflows": {"overall": {"COMPLETED": 3}, "closed_reason": {}, "expand_carried_forward": 0, "needs_human": {"open": 4, "overdue": 1}}}
    text = "\n".join(format_outcome(o))
    assert "集計時間 2.5秒" in text and "（集計 1.25秒）" in text and "選択可能な出典（後退中でない）: 12件" in text
    assert "取り直し24h見送りの影響" in text and "120頁を他の候補へ再配分" in text and "LINKED=2" in text and "人の判断待ち(needs-human) 4件（期限切れ1件）" in text
