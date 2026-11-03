"""出典の分割（価格帯パーティション）。純粋ロジックのみ（DB・ブラウザ・設定ファイルに依存しない）。

セラーのストアフロント検索は、結果が多いと取り切れない。そこで検索結果を価格帯で区切り、
取り切れない帯だけをさらに二分割して再取得する。本モジュールはその「帯」の作り方・割り方・
URLとの相互変換だけを提供する。呼び出し側（source_runner等）への接続は別段階。

単位: 価格は「円×100」（Amazonの検索パラメータ p_36 の単位）。帯は半開区間 [lo, hi)、hi=None は上限なし。
URL: `p_36=<lo>-<hi-1>`。hi=None のときは上限 99,999,999 円相当を使う。
登録: REGISTRY[(marketplace, source_type)]。登録が無い組は分割しない（USは登録しない）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence
from urllib.parse import unquote

# 価格の単位換算（円 → p_36の単位）
YEN_UNIT = 100
# hi=None（上限なし）をURLに書くときの上限（円）
UNBOUNDED_MAX_YEN = 99_999_999
UNBOUNDED_MAX = UNBOUNDED_MAX_YEN * YEN_UNIT

DEFAULT_INITIAL_EDGES_YEN: tuple[int, ...] = (
    0, 1000, 2000, 3000, 4000, 5000, 7500, 10000, 15000, 25000, 50000,
)
DEFAULT_MIN_WIDTH_YEN = 100
DEFAULT_MAX_DEPTH = 8


class PartitionError(ValueError):
    """分割設定・帯の不正。"""


class CoverError(PartitionError):
    """帯の列が範囲を隙間なく重ならず覆っていない。"""


@dataclass(frozen=True)
class Band:
    """価格帯 [lo, hi)。単位は円×100。hi=None は上限なし。depthは初期帯からの分割回数。"""

    lo: int
    hi: int | None
    depth: int = 0

    def __post_init__(self) -> None:
        if self.lo < 0:
            raise PartitionError(f"lo は0以上: {self.lo}")
        if self.hi is not None and self.hi <= self.lo:
            raise PartitionError(f"hi は lo より大きい必要がある: [{self.lo}, {self.hi})")
        if self.depth < 0:
            raise PartitionError(f"depth は0以上: {self.depth}")

    @property
    def sort_key(self) -> tuple[int]:
        """処理順（lo昇順）の比較キー。帯が重ならないので lo だけで全順序になる。"""
        return (self.lo,)

    @property
    def unbounded(self) -> bool:
        return self.hi is None

    def contains(self, price: int) -> bool:
        return self.lo <= price and (self.hi is None or price < self.hi)

    def __str__(self) -> str:
        return f"[{self.lo},{'inf' if self.hi is None else self.hi})"


@dataclass(frozen=True)
class PartitionConfig:
    initial_edges_yen: tuple[int, ...] = DEFAULT_INITIAL_EDGES_YEN
    min_width_yen: int = DEFAULT_MIN_WIDTH_YEN
    max_depth: int = DEFAULT_MAX_DEPTH
    # null=使わない。整数Nのとき、帯の末尾（「次へ」が消えた）が N ページ以上の帯なら、検索結果の深さの限界(DEPTH_LIMIT)と同じに扱う
    # （限界で「同じページの繰り返し」ではなく「次へが消える」形になる場合への備え。実ページでは未確認なので既定は使わない）
    treat_end_as_depth_limit_pages: int | None = None

    def __post_init__(self) -> None:
        edges = tuple(self.initial_edges_yen)
        object.__setattr__(self, "initial_edges_yen", edges)
        if not edges:
            raise PartitionError("initial_edges_yen が空")
        if edges[0] < 0:
            raise PartitionError("initial_edges_yen は0以上")
        if any(b <= a for a, b in zip(edges, edges[1:])):
            raise PartitionError("initial_edges_yen は狭義の昇順である必要がある")
        if self.min_width_yen < 1:
            raise PartitionError("min_width_yen は1以上")
        if self.max_depth < 0:
            raise PartitionError("max_depth は0以上")
        n = self.treat_end_as_depth_limit_pages
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            raise PartitionError("treat_end_as_depth_limit_pages は1以上の整数またはnull")

    @property
    def min_width(self) -> int:
        return self.min_width_yen * YEN_UNIT


class PartitionStrategy(Protocol):
    axis: str

    def applies(self, marketplace: str, source_type: str) -> bool: ...
    def initial(self, cfg: PartitionConfig | None = None) -> list[Band]: ...
    def split(self, band: Band) -> tuple[Band, Band] | None: ...
    def url(self, base_url: str, band: Band) -> str: ...
    def band_of_url(self, url: str) -> Band | None: ...


_P36_PARAM_RE = re.compile(r"[?&]p_36=[^&#]*")


def _strip_p36_param(url: str) -> str:
    """既存の `p_36=...` クエリパラメータを取り除く（rh内のp_36%3Aには触れない）。"""
    out = _P36_PARAM_RE.sub(lambda m: "?" if m.group(0)[0] == "?" else "", url)
    # "?"だけ残った/"?&"になった場合の整形
    out = out.replace("?&", "?")
    return out[:-1] if out.endswith(("?", "&")) else out
# `p_36=1-2`（クエリ）と `rh=...p_36%3A1-2`（デコード後 `p_36:1-2`）の両方。どちらも片側欠けを許す。
_P36_READ_RE = re.compile(r"p_36[=:](\d*)-(\d*)")


class PriceBandPartition:
    """価格(p_36)軸での分割。"""

    axis = "price"
    SUPPORTED: frozenset[tuple[str, str]] = frozenset({("JP", "SELLER")})

    def __init__(self, cfg: PartitionConfig | None = None) -> None:
        self.cfg = cfg or PartitionConfig()

    def applies(self, marketplace: str, source_type: str) -> bool:
        return (str(marketplace).upper(), str(source_type).upper()) in self.SUPPORTED

    def initial(self, cfg: PartitionConfig | None = None) -> list[Band]:
        c = cfg or self.cfg
        edges = [e * YEN_UNIT for e in c.initial_edges_yen]
        bands = [Band(lo, hi) for lo, hi in zip(edges, edges[1:])]
        bands.append(Band(edges[-1], None))
        return sorted(bands, key=lambda b: b.sort_key)

    def split(self, band: Band) -> tuple[Band, Band] | None:
        """二分割。割れないときは None。決定的（整数演算のみ）。"""
        if band.depth >= self.cfg.max_depth:
            return None
        step = self.cfg.min_width
        lo, hi = band.lo, band.hi
        if hi is not None and hi - lo <= step:
            return None
        if hi is None:
            if lo == 0:
                return None  # 倍にする基準が無い
            mid = 2 * lo
        elif lo == 0:
            mid = hi // 2
        else:
            mid = math.isqrt(lo * hi)  # 幾何平均（整数）
        mid = ((mid + step // 2) // step) * step  # 刻みに丸め（四捨五入）
        # lo < mid < hi を保証: 刻みの倍数のうち lo より大きい最小・hi より小さい最大に収める
        lowest = (lo // step + 1) * step
        highest = None if hi is None else ((hi - 1) // step) * step
        if highest is not None and lowest > highest:
            return None
        mid = max(mid, lowest)
        if highest is not None:
            mid = min(mid, highest)
        d = band.depth + 1
        return Band(lo, mid, d), Band(mid, hi, d)

    def url(self, base_url: str, band: Band) -> str:
        upper = UNBOUNDED_MAX if band.hi is None else band.hi - 1
        base = _strip_p36_param(base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}p_36={band.lo}-{upper}"

    def band_of_url(self, url: str) -> Band | None:
        m = _P36_READ_RE.search(unquote(url))
        if not m:
            return None
        lo_s, hi_s = m.groups()
        lo = int(lo_s) if lo_s else 0
        if not hi_s:
            return Band(lo, None)
        upper = int(hi_s)
        if upper >= UNBOUNDED_MAX:
            return Band(lo, None)
        if upper < lo:
            return None
        return Band(lo, upper + 1)


REGISTRY: dict[tuple[str, str], PartitionStrategy] = {
    ("JP", "SELLER"): PriceBandPartition(),
}


def get_strategy(marketplace: str, source_type: str) -> PartitionStrategy | None:
    """登録が無ければ None（分割しない）。"""
    return REGISTRY.get((str(marketplace).upper(), str(source_type).upper()))


def validate_markets(markets: Iterable[str], source_type: str = "SELLER") -> None:
    """分割を有効にする市場の設定を検証する。登録の無い市場（USなど）は拒否する。"""
    for m in markets:
        if get_strategy(m, source_type) is None:
            raise PartitionError(
                f"価格帯分割は {m}/{source_type} では使えない（登録されているのはJP/SELLERのみ）"
            )


def check_cover(
    bands: Sequence[Band], lo: int = 0, hi: int | None = None
) -> bool:
    """帯の列が [lo, hi)（hi=Noneは上限なし）を隙間なく・重ならず覆うことを検証する。

    順不同の入力を許す（lo昇順に並べて検査）。満たさなければ CoverError。
    """
    if not bands:
        raise CoverError("帯が空")
    ordered = sorted(bands, key=lambda b: b.sort_key)
    if ordered[0].lo != lo:
        raise CoverError(f"先頭が範囲の下限と一致しない: {ordered[0]} (下限 {lo})")
    for a, b in zip(ordered, ordered[1:]):
        if a.hi is None:
            raise CoverError(f"上限なし帯の後ろに帯がある: {a} の次に {b}")
        if a.hi < b.lo:
            raise CoverError(f"隙間: {a} と {b}")
        if a.hi > b.lo:
            raise CoverError(f"重なり: {a} と {b}")
    if ordered[-1].hi != hi:
        raise CoverError(f"末尾が範囲の上限と一致しない: {ordered[-1]} (上限 {hi})")
    return True
