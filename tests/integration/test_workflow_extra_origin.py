"""レーン設定 extra_origin_imports の結合テスト（実PostgreSQL、使い捨てtest-db、架空データ・疑似ブラウザのみ）。docs/DEVIATIONS.md #100。

USレーン(origin_import=US報告)に JP報告(import)をextraとして指定すると、JP報告由来の経路しか持たないUS市場の出典（従来はNO_ORIGINで候補外）が
取得候補になり、投入はその出典の経路の方向(IMPORT)で作られること、二重に取得されないこと、不正なextraは該当レーンだけを止めることを確かめる。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from acquisition.plan import build_plan
from acquisition.registry import SourceRegistry
from origin_selection.report_import import ReportImportService
from tests.integration.acq_helpers import make_active_report
from tests.integration.test_workflow import (  # noqa: F401  (fixture: own, world)
    World, own, statuses, submit_tasks, uid, world,
)
from tests.integration.test_workflow_acquire_independent import _discover_ends_with, _fetched, _runs
from workflow.config import config_from_dict

pytestmark = pytest.mark.integration


def _jp_derived_us_source(world: World) -> int:
    """US市場の出典で、JP報告(import)由来の経路（IMPORT）しか持たないもの（実データの「NO_ORIGIN」の再現）。"""
    seller = uid("AVJ")
    rp = SourceRegistry(world.f, actor="tester").register_path("US", "SELLER", seller, "", origin_import_id=world.jp_import, origin_asin=world.jp_top)
    world.us.seller_asins[seller] = ["B0" + uuid.uuid4().hex[:8].upper() for _ in range(2)]
    world.via_seller = seller
    return rp.source.source_id


def _cfg(world: World, us_extra=None, jp_extra=None):
    lanes = {"US": {"origin_import": world.us_import}, "JP": {"origin_import": world.jp_import}}
    if us_extra is not None:
        lanes["US"]["extra_origin_imports"] = us_extra
    if jp_extra is not None:
        lanes["JP"]["extra_origin_imports"] = jp_extra
    return config_from_dict({**world.cfg_dict, "lanes": lanes})


def _run(world: World, cfg):
    eng = world.engine(cfg)
    return world.run(eng)


def test_default_leaves_the_jp_derived_source_out_and_extra_brings_it_in(world, app_role_factory):
    sid = _jp_derived_us_source(world)
    reports = ReportImportService(app_role_factory, None)
    base = build_plan(app_role_factory, reports, "US", [world.us_import])
    assert sid in [e.facts.source_id for e in base.sections.get("NO_ORIGIN", [])]       # 従来: NO_ORIGINで候補外
    both = build_plan(app_role_factory, reports, "US", [world.us_import, world.jp_import])
    imp = [e for e in both.sections.get("IMPORT", []) if e.facts.source_id == sid]
    assert imp and imp[0].rep.import_id == world.jp_import and imp[0].rep.direction == "IMPORT"
    assert sid not in [e.facts.source_id for e in both.sections.get("NO_ORIGIN", [])]


def test_default_config_does_not_fetch_it(world, monkeypatch):
    sid = _jp_derived_us_source(world)
    _discover_ends_with(monkeypatch, "SERVER_ERROR")
    eng = _run(world, _cfg(world))
    assert _runs(world, "US", sid) == 0 and str(sid) not in eng.st.tasks["acquire:US"].result["sources"]


def test_extra_fetches_it_once_and_submits_in_the_direction_of_its_path(world, monkeypatch):
    sid = _jp_derived_us_source(world)
    _discover_ends_with(monkeypatch, "SERVER_ERROR")
    eng = _run(world, _cfg(world, us_extra=[world.jp_import]))
    assert statuses(eng)["acquire:US"] == "DONE", eng.st.tasks["acquire:US"].detail
    assert list(_fetched(eng, "US")) == [str(sid)] and _runs(world, "US", sid) == 1
    subs = [t for t in submit_tasks(eng) if t.lane == "US" and t.result["source_id"] == sid]
    assert subs and {t.result["direction"] for t in subs} == {"IMPORT"}              # IMPORTの出典がEXPORTとして投入されない
    assert all(t.result["business_market"] == "JP" and t.result["memo"] for t in subs)                # 業務方向IMPORT=JP市場の出品
    # crossはextra由来の出典を対象にしない（origin_importの起点経路が無い）: 反対側展開が保留のままにならない
    assert statuses(eng)["cross:US"] == "DONE"
    # JPレーンは従来どおり（extraなし）
    assert statuses(eng)["acquire:JP"] in ("DONE", "HELD", "FAILED", "SKIPPED") and str(sid) not in eng.st.tasks["acquire:JP"].result["sources"]


def test_a_second_workflow_does_not_fetch_the_same_source_again_within_the_interval(world, monkeypatch):
    sid = _jp_derived_us_source(world)
    _discover_ends_with(monkeypatch, "SERVER_ERROR")
    cfg = _cfg(world, us_extra=[world.jp_import])
    _run(world, cfg)
    assert _runs(world, "US", sid) == 1
    eng2 = _run(world, cfg)                                                           # 別のworkflow（取得間隔=30日の内側）
    assert _runs(world, "US", sid) == 1 and str(sid) not in _fetched(eng2, "US")
    with world.f.get_session("US") as conn, conn.begin():                             # 同時に実行中のrunは出典につき最大1件（lease一意制約）
        n = conn.execute(text("SELECT count(*) FROM collector_run WHERE source_id = :s AND status IN ('RUNNING','CAPTCHA_WAIT')"), {"s": sid}).scalar_one()
    assert n == 0


def test_invalid_extra_stops_only_its_own_lane(world, tmp_path, monkeypatch):
    sid = _jp_derived_us_source(world)
    other_us = make_active_report(world.f, tmp_path, "US", [("B0" + uuid.uuid4().hex[:8].upper(), 10, 1.0, 3)], name="us2.csv")
    _discover_ends_with(monkeypatch, "SERVER_ERROR")
    # USレーンのextraにUS市場のレポートを指定（不正）。JPレーンには正しいextra（US報告）を指定
    eng = _run(world, _cfg(world, us_extra=[other_us], jp_extra=[world.us_import]))
    s = statuses(eng)
    assert s["discover:US"] == "HELD" and _runs(world, "US", sid) == 0                # USレーンは取得に進まない
    assert s["acquire:JP"] in ("DONE", "HELD") and s["discover:JP"] != "HELD"          # JPレーンは影響を受けない
