"""出典キーの正規化と構造化キーのハッシュ。

キーは `key_primary` と `key_qualifier` の2列で持つ（連結した文字列にしない）。値に区切り文字が
含まれても衝突しない。一意性は、種別と両キーを長さ付きで連結したSHA-256（`key_hash`）で担保する
（長いキーワードでもPostgreSQLのインデックスサイズ制限に当たらない）。元の値は列に保持し、
照合時にhashだけでなく実際の値も比較して衝突を検査する（`KeyCollisionError`）。

自動で同一視するのは**表記の揺れ**だけ: 全角/半角（NFKC）、大文字小文字、空白の連続・前後。
自動で統合しないもの: 日英の表記違い、カッコ付きの複合表記、複数形、ブランド名とストア、
別のストア同士。これらの同一視は人が承認した別名表（段階2では作らない）でのみ行う。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse

SOURCE_TYPES = ("SELLER", "BRAND_NAME", "BRAND_STORE", "KEYWORD")


class KeyCollisionError(Exception):
    """key_hashが同じなのに元の値が異なる（SHA-256の衝突）。到達しない想定の検査。"""


def norm_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()


_STORE_PAGE_ID = re.compile(r"/page/([0-9a-z][0-9a-z-]{15,})", re.IGNORECASE)


def canonical_store_id(value: str) -> str:
    """ストアURL（または識別子）から、追跡パラメータを除いた安定部分を取り出す。
    `/page/<ID>` があればそれ、無ければ小文字化したパス。ブランド名とは別物として扱う。"""
    raw = unicodedata.normalize("NFKC", value).strip()
    if "://" in raw or raw.startswith("/") or raw.startswith("www."):
        parsed = urlparse(raw if "://" in raw else "https://" + raw.lstrip("/"))
        path = parsed.path
    else:
        path = raw
    m = _STORE_PAGE_ID.search(path)
    if m:
        return "page:" + m.group(1).casefold()
    return "path:" + path.strip("/").casefold()


def normalize_key(source_type: str, primary: str, qualifier: str = "") -> tuple[str, str]:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source_type: {source_type!r}")
    if source_type == "SELLER":
        key = unicodedata.normalize("NFKC", primary).strip().upper()
        qual = ""
    elif source_type == "KEYWORD":
        key = norm_text(primary)
        qual = norm_text(qualifier or "")
    elif source_type == "BRAND_NAME":
        key = norm_text(primary)
        qual = ""
    else:  # BRAND_STORE
        key = canonical_store_id(primary)
        qual = ""
    if not key or key in ("page:", "path:"):
        raise ValueError(f"empty key after normalization: {primary!r}")
    return key, qual


def key_hash(source_type: str, key_primary: str, key_qualifier: str = "") -> str:
    """種別・主キー・修飾キーを長さ付きで連結してSHA-256（区切り文字を含む値でも衝突しない）。"""
    h = hashlib.sha256()
    for part in (source_type, key_primary, key_qualifier):
        data = part.encode("utf-8")
        h.update(str(len(data)).encode("ascii") + b":" + data + b";")
    return h.hexdigest()
