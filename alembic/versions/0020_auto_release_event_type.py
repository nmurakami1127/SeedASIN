"""acquisition_source_event: allow event_type 'AUTO_RELEASE' (audit record of an automatic hold recovery)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-03

自動保留の自動解除（docs/DEVIATIONS.md #86）の監査記録のため、`acquisition_source_event.event_type` のCHECK制約 `ck_ase_type` に
'AUTO_RELEASE'（12文字。列は VARCHAR(14) のまま収まる）を加える。**変更はこの制約1つだけ**:
- 列の型・他の制約・索引・RLS・GRANT/REVOKE・トリガーは変更しない（追記専用: app_role は SELECT, INSERT のみ。UPDATE/DELETEは付与しない）。
- 既存の行は変更しない（許可する値が増えるだけなので、既存の全行は新しい制約も満たす）。
- 動作は変わらない: この制約が許すだけで、AUTO_RELEASEを書く経路は `python -m workflow.cli recovery-run --apply`（既定で無効）だけ。

全文が冪等（DROP CONSTRAINT IF EXISTS → ADD CONSTRAINT）。注意: op.execute(str)はtext()経由のため、文中に`:name`形式を書かない。

downgrade（事前にpg_dumpすること）:
- AUTO_RELEASEの行が1件でもあれば**中止する**（何も変更しない）。このテーブルは追記専用で、行を書き換える（'RELEASE'に読み替える）・削除する
  には app_role の権限を超える操作（または監査記録の改ざん）が必要になり、さらに「30日で2回」の自動解除の上限の根拠（件数）が失われるため。
  （0019のdowngradeが、途中の帯がある間は中止するのと同じ流儀: 失われると取り返しのつかない情報があるときは、黙って捨てない。）
- AUTO_RELEASEの行が無いときだけ、制約を0016の元の定義（6種類）へ戻す。
"""
from alembic import op
from sqlalchemy import text

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_TABLE = "acquisition_source_event"
_CONSTRAINT = "ck_ase_type"
_OLD = "'REGISTERED','HOLD','AUTO_HOLD','EXCLUDE','RELEASE','INTERVAL_SET'"
_NEW = "'REGISTERED','HOLD','AUTO_HOLD','EXCLUDE','RELEASE','INTERVAL_SET','AUTO_RELEASE'"


def _set_constraint(values: str) -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (event_type IN ({values}))")


def upgrade() -> None:
    _set_constraint(_NEW)


def downgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(text(f"SELECT to_regclass('{_TABLE}') IS NOT NULL")).scalar_one():
        return
    n = bind.execute(text(f"SELECT count(*) FROM {_TABLE} WHERE event_type = 'AUTO_RELEASE'")).scalar_one()
    if n:
        raise RuntimeError(
            f"0020のdowngradeを中止しました: AUTO_RELEASE（自動解除の監査記録）が{n}件あります。"
            "このテーブルは追記専用で、記録を'RELEASE'に読み替える・削除すると監査記録と「30日で2回」の上限の根拠が失われるため、"
            "自動では戻しません。内容をpg_dump等で退避し、人が確認したうえで対処してから再実行してください。（何も変更していません）"
        )
    _set_constraint(_OLD)
