"""acquisition.partition の単体テスト（DB不要）。"""
from __future__ import annotations

import random

import pytest

from acquisition.partition import (
    DEFAULT_INITIAL_EDGES_YEN,
    REGISTRY,
    UNBOUNDED_MAX,
    Band,
    CoverError,
    PartitionConfig,
    PartitionError,
    PriceBandPartition,
    check_cover,
    get_strategy,
    validate_markets,
)

Y = 100  # 円 -> p_36
BASE = "https://www.amazon.co.jp/s?me=A1B2C3D4E5"


def _split_all_randomly(strategy, bands, rng, rounds):
    bands = list(bands)
    for _ in range(rounds):
        i = rng.randrange(len(bands))
        r = strategy.split(bands[i])
        if r is not None:
            bands[i:i + 1] = list(r)
    return bands


# ---- 初期帯 ----

def test_default_initial_is_11_bands_covering_all():
    s = PriceBandPartition()
    bands = s.initial()
    assert len(bands) == 11
    assert [b.lo for b in bands] == [e * Y for e in DEFAULT_INITIAL_EDGES_YEN]
    assert bands[0] == Band(0, 1000 * Y)
    assert bands[-1] == Band(50000 * Y, None)
    assert check_cover(bands)
    assert all(b.depth == 0 for b in bands)


def test_initial_edges_configurable():
    s = PriceBandPartition(PartitionConfig(initial_edges_yen=(0, 500, 5000)))
    bands = s.initial()
    assert bands == [Band(0, 500 * Y), Band(500 * Y, 5000 * Y), Band(5000 * Y, None)]
    # 引数のcfgが優先
    assert len(s.initial(PartitionConfig(initial_edges_yen=(0,)))) == 1


@pytest.mark.parametrize("edges", [(), (1000, 500), (0, 0), (-1, 5)])
def test_invalid_edges_rejected(edges):
    with pytest.raises(PartitionError):
        PartitionConfig(initial_edges_yen=edges)


def test_invalid_config_values():
    with pytest.raises(PartitionError):
        PartitionConfig(min_width_yen=0)
    with pytest.raises(PartitionError):
        PartitionConfig(max_depth=-1)


def test_band_validation_and_order():
    with pytest.raises(PartitionError):
        Band(5, 5)
    with pytest.raises(PartitionError):
        Band(-1, 5)
    bs = [Band(300, None), Band(0, 100), Band(100, 300)]
    assert [b.lo for b in sorted(bs, key=lambda b: b.sort_key)] == [0, 100, 300]
    assert Band(0, 10).contains(0) and not Band(0, 10).contains(10)
    assert Band(10, None).contains(10**12)


# ---- 二分割 ----

def test_split_geometric_mean_rounded():
    s = PriceBandPartition()
    lo, hi = 1000 * Y, 4000 * Y  # 幾何平均 2000円
    a, b = s.split(Band(lo, hi))
    assert a == Band(lo, 2000 * Y, 1) and b == Band(2000 * Y, hi, 1)


def test_split_rounds_to_min_width():
    s = PriceBandPartition()
    a, b = s.split(Band(1000 * Y, 2000 * Y))  # sqrt(2e6)=1414.2円 -> 1400円
    assert a.hi == b.lo == 1400 * Y


def test_split_lo_zero_uses_half():
    s = PriceBandPartition()
    a, b = s.split(Band(0, 1000 * Y))
    assert a == Band(0, 500 * Y, 1) and b == Band(500 * Y, 1000 * Y, 1)


def test_split_unbounded_doubles():
    s = PriceBandPartition()
    a, b = s.split(Band(50000 * Y, None))
    assert a == Band(50000 * Y, 100000 * Y, 1)
    assert b == Band(100000 * Y, None, 1)


def test_split_unbounded_lo_zero_not_splittable():
    assert PriceBandPartition().split(Band(0, None)) is None


def test_split_is_deterministic():
    s1, s2 = PriceBandPartition(), PriceBandPartition()
    band = Band(7500 * Y, 10000 * Y)
    assert s1.split(band) == s1.split(band) == s2.split(band)


def test_split_min_width_boundary():
    s = PriceBandPartition()
    step = 100 * Y
    assert s.split(Band(5000 * Y, 5000 * Y + step)) is None  # 幅 == min_width
    r = s.split(Band(5000 * Y, 5000 * Y + 2 * step))  # 幅 2*min_width
    assert r is not None
    a, b = r
    assert a.hi == b.lo == 5000 * Y + step


def test_split_narrow_but_above_min_width_keeps_order():
    s = PriceBandPartition()
    # 幅が min_width を少し超える・刻みの倍数に乗らない端点
    band = Band(1000 * Y + 30, 1000 * Y + 30 + 100 * Y + 1)
    r = s.split(band)
    if r is not None:
        a, b = r
        assert a.lo < a.hi == b.lo < b.hi


def test_split_no_multiple_inside_returns_none():
    s = PriceBandPartition(PartitionConfig(min_width_yen=1))
    # 刻み(100)の倍数が内部に無い帯
    assert s.split(Band(10001, 10050)) is None  # 幅49 <= 100 なのでそもそも割れない
    s2 = PriceBandPartition(PartitionConfig(min_width_yen=1))
    # 幅 > step だが内部に倍数が無い、は構造上起きない（幅>step なら必ず倍数を含む）
    r = s2.split(Band(10001, 10201))
    assert r is not None and 10001 < r[0].hi < 10201


def test_split_max_depth_respected():
    s = PriceBandPartition(PartitionConfig(max_depth=2))
    band = Band(0, 10**9)
    for _ in range(2):
        band = s.split(band)[0]
    assert band.depth == 2
    assert s.split(band) is None
    s0 = PriceBandPartition(PartitionConfig(max_depth=0))
    assert s0.split(Band(0, 10**9)) is None


def test_default_max_depth_is_8():
    s = PriceBandPartition()
    band = Band(0, 10**12)
    depth = 0
    while True:
        r = s.split(band)
        if r is None:
            break
        band = r[1]
        depth += 1
    assert depth == 8


@pytest.mark.parametrize("seed", range(20))
def test_random_splits_keep_cover_no_gap_no_overlap(seed):
    rng = random.Random(seed)
    s = PriceBandPartition(
        PartitionConfig(min_width_yen=rng.choice([1, 10, 100, 500]), max_depth=rng.choice([3, 8, 20]))
    )
    initial = s.initial()
    bands = _split_all_randomly(s, initial, rng, rounds=300)
    assert check_cover(bands)
    # 全ての子は lo<hi、価格の取りこぼし・重複が無い（標本点で所属がちょうど1つ）
    for _ in range(300):
        price = rng.randrange(0, 200_000 * Y)
        assert sum(b.contains(price) for b in bands) == 1
    assert max(b.depth for b in bands) <= s.cfg.max_depth


def test_random_splits_cover_with_odd_custom_edges():
    rng = random.Random(1234)
    s = PriceBandPartition(PartitionConfig(initial_edges_yen=(0, 37, 1234, 99999), min_width_yen=7))
    bands = _split_all_randomly(s, s.initial(), rng, rounds=500)
    assert check_cover(bands)


# ---- check_cover ----

def test_check_cover_detects_gap_overlap_and_ends():
    with pytest.raises(CoverError):
        check_cover([Band(0, 10), Band(11, None)])  # 隙間
    with pytest.raises(CoverError):
        check_cover([Band(0, 10), Band(9, None)])  # 重なり
    with pytest.raises(CoverError):
        check_cover([Band(1, 10), Band(10, None)])  # 先頭
    with pytest.raises(CoverError):
        check_cover([Band(0, 10), Band(10, 20)])  # 末尾(上限なしが要る)
    with pytest.raises(CoverError):
        check_cover([Band(0, None), Band(5, None)])
    with pytest.raises(CoverError):
        check_cover([])
    assert check_cover([Band(10, None), Band(0, 10)])  # 順不同でもよい
    assert check_cover([Band(5, 10), Band(10, 20)], lo=5, hi=20)


# ---- URL ----

def test_url_appends_p36_to_me_url():
    s = PriceBandPartition()
    assert s.url(BASE, Band(1000 * Y, 2000 * Y)) == f"{BASE}&p_36=100000-199999"
    assert s.url("https://www.amazon.co.jp/s", Band(0, 10)) == "https://www.amazon.co.jp/s?p_36=0-9"


def test_url_unbounded_uses_99999999_yen():
    s = PriceBandPartition()
    u = s.url(BASE, Band(5_000_000, None))
    assert u.endswith("p_36=5000000-9999999900")
    assert UNBOUNDED_MAX == 99_999_999 * 100


def test_url_replaces_existing_p36():
    s = PriceBandPartition()
    u = s.url(BASE + "&p_36=1-2&x=1", Band(0, 100))
    assert u.count("p_36=") == 1 and u.endswith("p_36=0-99") and "x=1" in u
    assert s.url("https://a/s?p_36=1-2", Band(0, 100)) == "https://a/s?p_36=0-99"


def test_band_of_url_reads_query_form():
    s = PriceBandPartition()
    assert s.band_of_url(f"{BASE}&p_36=100000-199999") == Band(100000, 200000)


def test_band_of_url_reads_rh_form():
    s = PriceBandPartition()
    u = "https://www.amazon.co.jp/s?i=merchant-items&me=A1&rh=p_36%3A100000-199999"
    assert s.band_of_url(u) == Band(100000, 200000)
    u2 = "https://www.amazon.co.jp/s?me=A1&rh=n%3A2%2Cp_36%3A0-9999"
    assert s.band_of_url(u2) == Band(0, 10000)


def test_band_of_url_none_cases_and_unbounded():
    s = PriceBandPartition()
    assert s.band_of_url(BASE) is None
    assert s.band_of_url(f"{BASE}&p_36=9-3") is None
    assert s.band_of_url(f"{BASE}&p_36=5000000-9999999900") == Band(5_000_000, None)
    assert s.band_of_url(f"{BASE}&p_36=5000000-") == Band(5_000_000, None)


@pytest.mark.parametrize("seed", range(5))
def test_url_roundtrip(seed):
    rng = random.Random(seed)
    s = PriceBandPartition()
    bands = _split_all_randomly(s, s.initial(), rng, rounds=100)
    for b in bands:
        got = s.band_of_url(s.url(BASE, b))
        assert (got.lo, got.hi) == (b.lo, b.hi)


# ---- 登録 ----

def test_registry_only_jp_seller():
    assert list(REGISTRY) == [("JP", "SELLER")]
    assert isinstance(get_strategy("JP", "SELLER"), PriceBandPartition)
    assert get_strategy("jp", "seller") is get_strategy("JP", "SELLER")


@pytest.mark.parametrize("key", [("US", "SELLER"), ("JP", "BRAND"), ("JP", "RANKING"), ("XX", "YY")])
def test_unregistered_means_no_partition(key):
    assert get_strategy(*key) is None


def test_applies():
    s = PriceBandPartition()
    assert s.applies("JP", "SELLER")
    assert not s.applies("US", "SELLER")
    assert not s.applies("JP", "BRAND")


def test_validate_markets_rejects_us():
    validate_markets(["JP"])
    with pytest.raises(PartitionError):
        validate_markets(["JP", "US"])
    with pytest.raises(PartitionError):
        validate_markets(["US"])
