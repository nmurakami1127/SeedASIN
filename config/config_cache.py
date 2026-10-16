"""M12 設定テーブルのTTL付きインメモリキャッシュ（実装指示書Phase 2）。

get()は「marketplace内で1行に定まる」設定テーブル（marketplace_account_config,
search_allocation_config, processing_limit_config, recheck_interval_config,
retry_config）向け。search_condition_configのようにmarketplace×source内で複数行
（config_id単位）が存在し得るテーブルはget_all()を使う（ユーザー確認済み、2026-09-02）。

呼び出しごとにDBへ問い合わせるのではなく、(table, key)単位でTTL付きにキャッシュする。
ConfigCacheはBaseRepository/RetryStateManagerと異なり、既存のトランザクションに参加する
必要がない読み取り専用の設定参照であるため、自身でSessionFactory.get_session()を呼び
出して短命の接続を都度開く設計にする。

**同一テーブルに対するget()とget_all()の混在は非対応。** get()のキャッシュキーは
(table, key)、get_all()のキャッシュキーは(table, filters)であり、両者は完全に独立した
エントリとして保存・失効する。同じテーブルの同じ行を両方の経路でキャッシュした場合、
片方だけTTLが失効して新しいDB値を取得し、もう片方は古いキャッシュ値を返す、という
不整合が起こり得る（get_all()で列挙した後にその中の1行だけget()で取得する、といった
使い方は特に注意）。Phase 2時点の対象6テーブルはget()専用（marketplace_account_config,
search_allocation_config, processing_limit_config, recheck_interval_config,
retry_config）とget_all()専用（search_condition_config）に完全に分かれており問題は
無いが、今後テーブルを追加する際はこの制約を踏まえ、1テーブルにつきget()/get_all()の
どちらか一方のみを使うこと（ユーザー確認済み、2026-09-02）。
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from sqlalchemy import select

import db.models as models
from db.session import Marketplace, SessionFactory

_CacheKey = tuple[str, frozenset[tuple[str, Any]]]


class ConfigCache:
    def __init__(self, session_factory: SessionFactory, marketplace: Marketplace) -> None:
        self._session_factory = session_factory
        self._marketplace = marketplace
        self._store: dict[_CacheKey, tuple[float, Any]] = {}

    def get(self, table: str, key: Mapping[str, Any] | None = None, ttl: int = 60) -> Mapping[str, Any] | None:
        """table内で(marketplace, key)により一意に定まる1行を返す（無ければNone）。"""
        cache_key = self._cache_key(table, key)
        cached = self._get_cached(cache_key, ttl)
        if cached is not _MISSING:
            return cached

        tbl = getattr(models, table)
        stmt = select(tbl).where(tbl.c.marketplace == self._marketplace)
        for col, val in (key or {}).items():
            stmt = stmt.where(tbl.c[col] == val)

        with self._session_factory.get_session(self._marketplace) as conn:
            row = conn.execute(stmt).mappings().first()
        value = dict(row) if row is not None else None
        self._store[cache_key] = (time.monotonic() + ttl, value)
        return value

    def get_all(
        self, table: str, filters: Mapping[str, Any] | None = None, ttl: int = 60
    ) -> list[Mapping[str, Any]]:
        """table内でmarketplace（＋filters）に該当する全行を返す（search_condition_config等）。"""
        cache_key = self._cache_key(table, filters)
        cached = self._get_cached(cache_key, ttl)
        if cached is not _MISSING:
            return cached

        tbl = getattr(models, table)
        stmt = select(tbl).where(tbl.c.marketplace == self._marketplace)
        for col, val in (filters or {}).items():
            stmt = stmt.where(tbl.c[col] == val)

        with self._session_factory.get_session(self._marketplace) as conn:
            rows = conn.execute(stmt).mappings().all()
        value = [dict(r) for r in rows]
        self._store[cache_key] = (time.monotonic() + ttl, value)
        return value

    def invalidate(self, table: str | None = None) -> None:
        """テストや設定変更直後の強制再取得用。table省略時は全キャッシュを破棄する。

        table指定時はget()由来・get_all()由来のエントリを区別せず、そのテーブルの
        キャッシュを全て破棄する（キーの先頭要素がtable名で共通のため）。"""
        if table is None:
            self._store.clear()
        else:
            self._store = {k: v for k, v in self._store.items() if k[0] != table}

    @staticmethod
    def _cache_key(table: str, key: Mapping[str, Any] | None) -> _CacheKey:
        return (table, frozenset((key or {}).items()))

    def _get_cached(self, cache_key: _CacheKey, ttl: int) -> Any:
        entry = self._store.get(cache_key)
        if entry is None:
            return _MISSING
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            return _MISSING
        return value


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()
