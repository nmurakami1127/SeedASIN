"""M11 データ保存基盤: DB接続確立直後にapp.current_marketplaceをSETするセッションファクトリ。

3つのDBロールに対応する3種のファクトリを提供する:
- SessionFactory: app_role / admin_role 用（RLSでmarketplaceスコープが効く。接続直後に必ずSETする）
- ReportSessionFactory: kpi_report_role 用（BYPASSRLSのためSETは行わない。KPI-09横断集計専用）
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Literal

from sqlalchemy import Connection, create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError

from errorctl.exceptions import RLSViolationError

Marketplace = Literal["US", "JP"]

_ENGINES: dict[str, Engine] = {}


def _get_engine(database_url: str) -> Engine:
    # 同一URLに対してはエンジン（コネクションプール）を使い回す。
    if database_url not in _ENGINES:
        _ENGINES[database_url] = create_engine(database_url, pool_pre_ping=True)
    return _ENGINES[database_url]


def _is_rls_unset_error(exc: ProgrammingError) -> bool:
    # app.current_marketplace未設定時、current_setting()（missing_ok省略）が
    # 'unrecognized configuration parameter' エラーを送出する（docs/DEVIATIONS.md #2）。
    return "unrecognized configuration parameter" in str(exc.orig)


class SessionFactory:
    """app_role / admin_role 用のセッションファクトリ。

    接続確立直後に必ず`SET app.current_marketplace`を発行する（CLAUDE.md 2節）。
    トランザクション境界は呼び出し元が`with conn.begin(): ...`で明示的に制御すること
    （M8/M4/M6等、モジュールごとに異なる粒度のトランザクションが要求されるため）。
    """

    def __init__(self, database_url: str) -> None:
        self._engine = _get_engine(database_url)

    @classmethod
    def for_app_role(cls) -> "SessionFactory":
        return cls(_require_env("SEEDASIN_APP_DATABASE_URL"))

    @classmethod
    def for_admin_role(cls) -> "SessionFactory":
        return cls(_require_env("SEEDASIN_ADMIN_DATABASE_URL"))

    @contextmanager
    def get_session(self, marketplace: Marketplace) -> Iterator[Connection]:
        if marketplace not in ("US", "JP"):
            raise ValueError(f"marketplace must be 'US' or 'JP', got: {marketplace!r}")

        conn = self._engine.connect()
        try:
            # marketplaceは上でUS/JPに限定済みのため文字列連結で安全（SET文はバインドパラメータ不可）。
            conn.execute(text(f"SET app.current_marketplace = '{marketplace}'"))
            # SQLAlchemy 2.xはexecute()でトランザクションをautobeginするため、ここでcommitして
            # おかないと呼び出し元がwith conn.begin(): できない。SET（非LOCAL）はセッション全体に
            # 効くため、ここでcommitしても設定値はコネクションが閉じるまで保持される。
            conn.commit()
            try:
                yield conn
            except ProgrammingError as exc:
                if _is_rls_unset_error(exc):
                    raise RLSViolationError() from exc
                raise
        finally:
            conn.close()


class ReportSessionFactory:
    """kpi_report_role 用（BYPASSRLS）。marketplaceのSETは行わず、全marketplaceの行が見える。

    KPI-09横断集計（M10 calc_cross_marketplace）とB7ステップ④のkpi_snapshot
    marketplace='ALL'行の書き込み以外では使用しないこと（CLAUDE.md 2節）。
    """

    def __init__(self, database_url: str) -> None:
        self._engine = _get_engine(database_url)

    @classmethod
    def for_kpi_report_role(cls) -> "ReportSessionFactory":
        return cls(_require_env("SEEDASIN_KPI_REPORT_DATABASE_URL"))

    @contextmanager
    def get_session(self) -> Iterator[Connection]:
        conn = self._engine.connect()
        try:
            yield conn
        finally:
            conn.close()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is required but not set")
    return value
