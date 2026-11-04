"""読み取り専用の確認用コマンド（outcome / plan-compare）のDB接続の解決（docs/DEVIATIONS.md #98）。

環境変数を設定していないPCでも動かすため、次の順で接続先を決める（**app_roleだけ**。admin/superuserは使わない）:
1. `SEEDASIN_APP_DATABASE_URL`（設定されていればそれ。本番・検証用の明示）
2. ローカル開発DB: docker-compose.ymlの`localhost:5432/seedasin`（`SEEDASIN_LOCAL_DB_HOST`/`_PORT`/`_NAME`で上書き可）に、
   alembic 0001が固定で作るローカル開発用のapp_roleパスワード（`_LOCAL_DEV_PASSWORDS`）で接続する。
   このパスワードはローカル開発専用（docs/DEVIATIONS.md #22）。本番で使う場合は1を設定する。
接続したあとは、いつもどおり`SessionFactory.get_session(market)`が`app.current_marketplace`を毎回SETする（RLSは迂回しない）。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_MIGRATION = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0001_create_roles.py"


def local_dev_app_url() -> str:
    spec = importlib.util.spec_from_file_location("_mig0001", _MIGRATION)
    if spec is None or spec.loader is None or not _MIGRATION.exists():
        raise RuntimeError("alembic/versions/0001_create_roles.py が見つからないため、ローカル開発用の接続先を決められません。SEEDASIN_APP_DATABASE_URL を設定してください")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pw = mod._LOCAL_DEV_PASSWORDS["app_role"]
    host = os.environ.get("SEEDASIN_LOCAL_DB_HOST", "localhost")
    port = os.environ.get("SEEDASIN_LOCAL_DB_PORT", "5432")
    name = os.environ.get("SEEDASIN_LOCAL_DB_NAME", "seedasin")
    return f"postgresql+psycopg://app_role:{pw}@{host}:{port}/{name}"


def resolve_app_url() -> tuple[str, str]:
    """(接続URL, 由来)。由来は表示用（パスワードは含めない）。"""
    env = os.environ.get("SEEDASIN_APP_DATABASE_URL")
    if env:
        return env, "env:SEEDASIN_APP_DATABASE_URL"
    return local_dev_app_url(), "local-dev:app_role@localhost（環境変数未設定のため、ローカル開発DBの既定を使用）"


def readonly_factory():
    """app_roleのSessionFactory（接続の解決だけを担う。以降の読み取りはREAD ONLYトランザクションで行う）。"""
    from db.session import SessionFactory

    url, _ = resolve_app_url()
    return SessionFactory(url)
