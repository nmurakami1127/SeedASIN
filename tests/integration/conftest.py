"""結合テスト共通フィクスチャ: 実PostgreSQL 16に接続してalembic upgrade/downgradeし、
app_role/kpi_report_role/admin_roleの3ロールのSessionFactory/ReportSessionFactoryを提供する。

前提: 使い捨て可能なPostgreSQL 16インスタンス（CIのservice container、または
`docker compose up -d test-db` で起動したローカルtest-db、**dbサービスではない**）に
対して実行する。個人の開発用DBを巻き込まないよう、環境変数を明示的に設定した場合のみ
実行する（デフォルトではスキップする）。

【重要】このフィクスチャは`alembic downgrade base`（全テーブル・全ロール削除）を
テストモジュールごとに実行する。過去に`docker compose`の`db`サービス（ローカル開発用、
実際にCLIで登録・収集したデータが溜まっていく想定のDB）を対象に結合テストを実行して
しまい、実データを繰り返し消してしまう事故があった（docs/DEVIATIONS.md #23参照）。
これを構造的に防ぐため、`_superuser_url()`は接続先のデータベース名が`seedasin_test`
（`test-db`サービス専用のDB名）であることを検証し、一致しなければ即座に失敗する
（`db`サービス・本番DB等を指すURLを渡しても、alembicコマンドを一切実行せず止まる）。

実行例:
    docker compose up -d test-db
    SEEDASIN_TEST_SUPERUSER_DATABASE_URL="postgresql+psycopg://seedasin:seedasin_local_dev@localhost:5433/seedasin_test" \\
        pytest tests/integration -m integration
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import db.session as db_session
from db.session import ReportSessionFactory, SessionFactory

_SUPERUSER_URL_ENV = "SEEDASIN_TEST_SUPERUSER_DATABASE_URL"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 結合テストの対象DB名はこれ以外を一切許容しない（docs/DEVIATIONS.md #23）。
# docker-compose.ymlのtest-dbサービスが払い出すデータベース名と一致させること。
_REQUIRED_TEST_DB_NAME = "seedasin_test"

_TEST_PASSWORDS = {
    "app_role": "test_app_role_pw",
    "kpi_report_role": "test_kpi_report_role_pw",
    "admin_role": "test_admin_role_pw",
}


def _superuser_url() -> str:
    url = os.environ.get(_SUPERUSER_URL_ENV)
    if not url:
        pytest.skip(
            f"{_SUPERUSER_URL_ENV} not set; integration tests require an explicit, "
            "disposable PostgreSQL 16 instance (this test drops/recreates roles and tables)."
        )

    db_name = make_url(url).database
    if db_name != _REQUIRED_TEST_DB_NAME:
        pytest.fail(
            f"{_SUPERUSER_URL_ENV} points at database {db_name!r}, not "
            f"{_REQUIRED_TEST_DB_NAME!r}. This fixture runs 'alembic downgrade base' "
            "(drops ALL tables and roles) against this database every test run — refusing "
            "to proceed against anything but the dedicated test database, to avoid ever "
            "destroying a real/local-dev database again (docs/DEVIATIONS.md #23). "
            "Use docker-compose.yml's test-db service (port 5433, database seedasin_test), "
            "never the db service (port 5432, database seedasin).",
            pytrace=False,
        )
    return url


def role_url(superuser_url: str, role: str) -> str:
    # str(URL)はURL.__str__()がパスワードを'***'にマスクするため使わない
    # （実際に'***'という文字列がパスワードとして送信され認証に失敗する。docs/DEVIATIONS.md #10）。
    url = make_url(superuser_url).set(username=role, password=_TEST_PASSWORDS[role])
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def superuser_url() -> str:
    url = _superuser_url()
    try:
        create_engine(url).connect().close()
    except OperationalError as exc:
        pytest.skip(f"cannot connect to {_SUPERUSER_URL_ENV}: {exc}")
    return url


def _dispose_cached_engines() -> None:
    # db.session._get_engine()はURL文字列単位でエンジン（コネクションプール）をプロセス全体で
    # 使い回す。本fixtureはmodule単位でDROP ROLE→CREATE ROLEを繰り返す（alembic downgrade/upgrade）
    # ため、他のテストモジュールが張ったままの物理コネクションがプールに残っていると、
    # 「同名だが別OIDになった新しいロール」に対する権限チェックを、古いロールOIDで認証済みの
    # コネクションで行おうとして permission denied になる。
    #
    # 実行順序には依存しない（ランダム化やxdist並列とは無関係のバグ）: 同一pytestプロセス内で
    # このfixtureを使うテストモジュールが2つ以上あれば、後から実行される方が必ず踏む。原因は
    # 純粋に「_ENGINESがプロセス全体でグローバル共有されている」ことであり、モジュールの実行順や
    # 本numberとは無関係。そのため`pytest-randomly`のような順序固定オプションやCI側の対応は
    # 不要で、対処はこのfixture（テストインフラの共通レイヤー）に一本化してある。今後
    # tests/integration/配下に新しいテストファイルを追加しても、このconftest.pyの
    # _migrated_schemaを使う限り自動的に保護される（2026-09-02, 複数結合テストファイルを
    # 同一pytestセッションで実行して再現・特定。docs/DEVIATIONS.md参照。本番運用ではDROP
    # ROLE/CREATE ROLEを行わないため実運用コードへの影響はない）。
    #
    # なお現時点でpytest-randomly/pytest-xdistは未導入（pip list, 2026-09-02確認）で、CIワーク
    # フロー自体も未整備。将来これらを導入する場合でも、本fixtureがプロセス単位でエンジンを
    # 破棄する限り、xdistの別プロセスワーカーはそもそも_ENGINESを共有しないため問題にならない
    # （同一ワーカー内で複数の結合テストファイルが動く場合のみ本fixtureの保護が効けば十分）。
    for engine in db_session._ENGINES.values():
        engine.dispose()
    db_session._ENGINES.clear()


def _clear_price_bands(superuser_url: str) -> None:
    """0019のdowngradeは取得途中(PENDING/ACTIVE)の帯があると中止する（仕様。docs/DEVIATIONS.md #82）。テストが残した帯や、前回の中断で
    残った帯で `downgrade base` が止まらないよう、テスト専用DBの帯だけを先に空にする（表が無ければ何もしない）。"""
    engine = create_engine(superuser_url)
    try:
        with engine.begin() as conn:
            if conn.execute(text("SELECT to_regclass('acquisition_band') IS NOT NULL")).scalar_one():
                conn.execute(text("TRUNCATE acquisition_band"))
    finally:
        engine.dispose()


def _clear_auto_release_events(superuser_url: str) -> None:
    """0020のdowngradeはAUTO_RELEASE（自動解除の監査記録）が1件でもあると中止する（仕様。docs/DEVIATIONS.md #86）。テストが残した記録で
    `downgrade base` が止まらないよう、テスト専用DBの記録だけをsuperuserで先に消す（表が無ければ何もしない。app_roleは削除できないまま）。"""
    engine = create_engine(superuser_url)
    try:
        with engine.begin() as conn:
            if conn.execute(text("SELECT to_regclass('acquisition_source_event') IS NOT NULL")).scalar_one():
                conn.execute(text("DELETE FROM acquisition_source_event WHERE event_type = 'AUTO_RELEASE'"))
    finally:
        engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(superuser_url: str):
    _dispose_cached_engines()
    _clear_price_bands(superuser_url)
    _clear_auto_release_events(superuser_url)

    env = os.environ.copy()
    env["SEEDASIN_DATABASE_URL"] = superuser_url
    # 前回の実行のteardown（downgrade base）が失敗・中断していると、スキーマと行が残ったまま `upgrade head` が何もせずに通り、
    # 前回の行（期限切れの実行中runなど）が次のテストモジュールへ漏れる（2026-10-18に実際に発生。docs/DEVIATIONS.md #64）。
    # 毎回、空の状態から作り直す。戻せない場合はここで失敗させ、汚れたDBでテストを続けない。
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"], check=True, cwd=_PROJECT_ROOT, env=env
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], check=True, cwd=_PROJECT_ROOT, env=env
    )

    engine = create_engine(superuser_url)
    with engine.begin() as conn:
        for role, pw in _TEST_PASSWORDS.items():
            conn.execute(text(f"ALTER ROLE {role} PASSWORD '{pw}'"))
    engine.dispose()

    yield

    _clear_price_bands(superuser_url)
    _clear_auto_release_events(superuser_url)
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"], check=True, cwd=_PROJECT_ROOT, env=env
    )

    _dispose_cached_engines()


@pytest.fixture()
def app_role_factory(superuser_url: str) -> SessionFactory:
    return SessionFactory(role_url(superuser_url, "app_role"))


@pytest.fixture()
def admin_role_factory(superuser_url: str) -> SessionFactory:
    return SessionFactory(role_url(superuser_url, "admin_role"))


@pytest.fixture()
def kpi_report_factory(superuser_url: str) -> ReportSessionFactory:
    return ReportSessionFactory(role_url(superuser_url, "kpi_report_role"))
