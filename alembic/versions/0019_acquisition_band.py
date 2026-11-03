"""acquisition_band: price bands of a cycle (stage C of the band-partitioned fetch)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03

価格帯で分割して取得する一巡の「帯」の台帳（docs/DEVIATIONS.md #82）。**新テーブル1つ（acquisition_band）だけ**を追加する。
既存テーブルの列・制約・トリガー・権限は変更しない。この段階では帯を使う経路（source_runner・workflow・planner）は無く、動作は変わらない。

列の意味:
- marketplace      取得先ページの市場（0016と同じ。RLSはこの列で区画する）
- source_id/cycle_id  どの出典の、どの一巡の帯か（複合外部キーで、出典・市場・一巡の組をDBが保証する）
- axis             分割の軸（今は 'PRICE' のみ）。lo_units/hi_units は軸の値の最小単位（価格ならセント・円）。[lo, hi) の半開区間、hi NULL は上限なし
- parent_band_id   分割の元の帯（同じ一巡の中だけ参照できる）。depth は分割の深さ（親なし=0）
- state            PENDING（未着手）→ ACTIVE（取得中。一巡に高々1つ）→ DONE（最後まで取得）/ SPLIT（さらに2つに分割した）/ TRUNCATED_MIN（これ以上分割できない最小幅で打ち切った）
- open_run_id/close_run_id  帯を開始・終了したrun（同じ一巡のrunに限る）。pages_fetched/asins_new は帯ごとの累計

保護（トリガー、collector_run_guardと同じ型）:
- INSERT: 新しい帯は PENDING・件数0でのみ作れる。親があれば SPLIT 済みで、同じ一巡・軸・深さ+1・親の範囲内であること。
  同じ一巡・軸で SPLIT 以外の帯と範囲が重なる帯は作れない（葉の帯は互いに重ならない）。
- UPDATE: 識別列（市場・出典・一巡・軸・範囲・親・深さ・作成時刻）は不変。state は PENDING→ACTIVE→{DONE,SPLIT,TRUNCATED_MIN} の一方向のみ
  （PENDING から DONE/SPLIT へは直行できない: 帯を開いたrunを必ず記録するため）。終端状態の行は変更不可。open_run_id は PENDING→ACTIVE で
  一度だけ入り以後不変。件数は減らせない。
- 一巡に ACTIVE は高々1つ（部分一意索引）。

【空DBと既存DBの両立】0014・0016と同じく、新テーブルは共有metadata／ALL_TABLESに入れず、生SQLだけでRLS・GRANTまで完結させる。
全文が冪等（IF NOT EXISTS / DROP ... IF EXISTS）。注意: op.execute(str)はtext()経由のため、文中に`:name`形式を書かない。

権限: app_role は SELECT, INSERT と列限定UPDATE（open_run_id, state, close_reason, close_run_id, pages_fetched, asins_new, closed_at）。DELETEなし。
kpi_report_role は SELECT（BYPASSRLS）。admin_role は付与なし。

downgrade（事前にpg_dumpすること）: PENDING／ACTIVE の帯が1つでも残っているとき（取得の途中の一巡があるとき）は中止する（何も変更しない）。
全ての帯が終端（DONE/SPLIT/TRUNCATED_MIN）のときだけ acquisition_band を削除する。失われるのは帯の履歴のみで、collector_run・collector_asinは変更しない。
"""
from alembic import op
from sqlalchemy import text

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_TABLE = "acquisition_band"

_GUARD = """
CREATE OR REPLACE FUNCTION acquisition_band_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.band_id IS DISTINCT FROM OLD.band_id OR NEW.marketplace IS DISTINCT FROM OLD.marketplace
       OR NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.cycle_id IS DISTINCT FROM OLD.cycle_id
       OR NEW.axis IS DISTINCT FROM OLD.axis OR NEW.lo_units IS DISTINCT FROM OLD.lo_units OR NEW.hi_units IS DISTINCT FROM OLD.hi_units
       OR NEW.parent_band_id IS DISTINCT FROM OLD.parent_band_id OR NEW.depth IS DISTINCT FROM OLD.depth
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'acquisition_band identity columns are immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.state IN ('DONE','SPLIT','TRUNCATED_MIN') THEN
        RAISE EXCEPTION 'acquisition_band % is already terminal (%) and cannot be modified', OLD.band_id, OLD.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT ((OLD.state = 'PENDING' AND NEW.state IN ('PENDING','ACTIVE'))
            OR (OLD.state = 'ACTIVE' AND NEW.state IN ('ACTIVE','DONE','SPLIT','TRUNCATED_MIN'))) THEN
        RAISE EXCEPTION 'acquisition_band % cannot change state from % to %', OLD.band_id, OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.state = 'ACTIVE' AND NEW.open_run_id IS DISTINCT FROM OLD.open_run_id THEN
        RAISE EXCEPTION 'acquisition_band.open_run_id is set once and immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.pages_fetched < OLD.pages_fetched OR NEW.asins_new < OLD.asins_new THEN
        RAISE EXCEPTION 'acquisition_band counters cannot decrease'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$
"""

_INSERT_GUARD = """
CREATE OR REPLACE FUNCTION acquisition_band_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    par RECORD;
BEGIN
    IF NEW.state <> 'PENDING' THEN
        RAISE EXCEPTION 'a new acquisition_band must start as PENDING (got %)', NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.parent_band_id IS NOT NULL THEN
        SELECT p.state, p.depth, p.axis, p.lo_units, p.hi_units, p.source_id, p.marketplace INTO par
        FROM acquisition_band p WHERE p.band_id = NEW.parent_band_id AND p.cycle_id = NEW.cycle_id;
        IF NOT FOUND OR par.state <> 'SPLIT' OR par.axis <> NEW.axis OR par.source_id <> NEW.source_id
           OR par.marketplace <> NEW.marketplace OR NEW.depth <> par.depth + 1
           OR NEW.lo_units < par.lo_units
           OR (par.hi_units IS NOT NULL AND (NEW.hi_units IS NULL OR NEW.hi_units > par.hi_units)) THEN
            RAISE EXCEPTION 'acquisition_band parent % is not a SPLIT band of the same cycle containing the new range', NEW.parent_band_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM acquisition_band o
        WHERE o.cycle_id = NEW.cycle_id AND o.axis = NEW.axis AND o.state <> 'SPLIT'
          AND (o.hi_units IS NULL OR NEW.lo_units < o.hi_units)
          AND (NEW.hi_units IS NULL OR o.lo_units < NEW.hi_units)
    ) THEN
        RAISE EXCEPTION 'acquisition_band range [%, %) overlaps an existing band of cycle %', NEW.lo_units, NEW.hi_units, NEW.cycle_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$
"""


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            band_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            marketplace CHAR(2) NOT NULL,
            source_id BIGINT NOT NULL,
            cycle_id BIGINT NOT NULL,
            axis VARCHAR(12) NOT NULL DEFAULT 'PRICE',
            lo_units BIGINT NOT NULL,
            hi_units BIGINT,
            parent_band_id BIGINT,
            depth SMALLINT NOT NULL DEFAULT 0,
            state VARCHAR(14) NOT NULL DEFAULT 'PENDING',
            close_reason VARCHAR(40),
            open_run_id BIGINT,
            close_run_id BIGINT,
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            asins_new INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ
        )
        """
    )
    # 外部キーを先に外してから、参照先の一意制約を作り直す（再実行しても通る）
    for name in ("fk_band_source", "fk_band_cycle", "fk_band_parent", "fk_band_open_run", "fk_band_close_run"):
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS uk_band_id_cycle")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT uk_band_id_cycle UNIQUE (band_id, cycle_id)")
    for name, fk in (
        ("fk_band_source", "FOREIGN KEY (source_id, marketplace) REFERENCES acquisition_source (source_id, marketplace)"),
        ("fk_band_cycle", "FOREIGN KEY (cycle_id, source_id, marketplace) REFERENCES collector_run (id, source_id, marketplace)"),
        ("fk_band_parent", "FOREIGN KEY (parent_band_id, cycle_id) REFERENCES acquisition_band (band_id, cycle_id)"),
        ("fk_band_open_run",
         "FOREIGN KEY (open_run_id, source_id, marketplace, cycle_id) REFERENCES collector_run (id, source_id, marketplace, cycle_id)"),
        ("fk_band_close_run",
         "FOREIGN KEY (close_run_id, source_id, marketplace, cycle_id) REFERENCES collector_run (id, source_id, marketplace, cycle_id)"),
    ):
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} {fk}")

    for name, expr in (
        ("ck_band_marketplace", "marketplace IN ('US','JP')"),
        ("ck_band_axis", "axis IN ('PRICE')"),
        ("ck_band_lo", "lo_units >= 0"),
        ("ck_band_range", "hi_units IS NULL OR hi_units > lo_units"),
        ("ck_band_depth", "depth >= 0 AND ((parent_band_id IS NULL) = (depth = 0))"),
        ("ck_band_state", "state IN ('PENDING','ACTIVE','DONE','SPLIT','TRUNCATED_MIN')"),
        ("ck_band_counters", "pages_fetched >= 0 AND asins_new >= 0"),
        ("ck_band_shape",
         "(state = 'PENDING' AND open_run_id IS NULL AND close_run_id IS NULL AND closed_at IS NULL AND close_reason IS NULL "
         "AND pages_fetched = 0 AND asins_new = 0) "
         "OR (state = 'ACTIVE' AND open_run_id IS NOT NULL AND close_run_id IS NULL AND closed_at IS NULL AND close_reason IS NULL) "
         "OR (state IN ('DONE','SPLIT','TRUNCATED_MIN') AND open_run_id IS NOT NULL AND close_run_id IS NOT NULL "
         "AND closed_at IS NOT NULL AND close_reason IS NOT NULL)"),
    ):
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({expr})")

    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uk_band_range ON {_TABLE} (cycle_id, axis, lo_units, (coalesce(hi_units, -1)))"
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uk_band_one_active ON {_TABLE} (cycle_id) WHERE state = 'ACTIVE'")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_band_cycle_state ON {_TABLE} (cycle_id, state, lo_units)")

    op.execute(_GUARD)
    op.execute(_INSERT_GUARD)
    op.execute(f"DROP TRIGGER IF EXISTS trg_acquisition_band_guard ON {_TABLE}")
    op.execute(
        f"CREATE TRIGGER trg_acquisition_band_guard BEFORE UPDATE ON {_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION acquisition_band_guard()"
    )
    op.execute(f"DROP TRIGGER IF EXISTS trg_acquisition_band_insert_guard ON {_TABLE}")
    op.execute(
        f"CREATE TRIGGER trg_acquisition_band_insert_guard BEFORE INSERT ON {_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION acquisition_band_insert_guard()"
    )

    # --- RLS（0016と同じ。current_settingはmissing_ok省略: 未設定ならPostgreSQLエラーで止める）---
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_marketplace_isolation ON {_TABLE}")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_marketplace_isolation ON {_TABLE}
        USING (marketplace = current_setting('app.current_marketplace'))
        WITH CHECK (marketplace = current_setting('app.current_marketplace'))
        """
    )

    # --- 権限 ---
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO app_role")
    op.execute(f"REVOKE DELETE ON {_TABLE} FROM app_role")
    op.execute(
        f"GRANT UPDATE (open_run_id, state, close_reason, close_run_id, pages_fetched, asins_new, closed_at) ON {_TABLE} TO app_role"
    )
    op.execute(f"GRANT SELECT ON {_TABLE} TO kpi_report_role")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(text(f"SELECT to_regclass('{_TABLE}') IS NOT NULL")).scalar_one():
        open_bands = bind.execute(
            text(f"SELECT count(*), count(DISTINCT cycle_id) FROM {_TABLE} WHERE state IN ('PENDING','ACTIVE')")
        ).one()
        if open_bands[0]:
            raise RuntimeError(
                f"0019のdowngradeを中止しました: 取得の途中（PENDING/ACTIVE）の帯が{open_bands[0]}件（{open_bands[1]}個の一巡）残っています。"
                "削除すると途中の位置が失われるため、該当の一巡を完了させるか、人が内容を確認して退避してから再実行してください。"
                "（何も変更していません）"
            )
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    op.execute("DROP FUNCTION IF EXISTS acquisition_band_guard()")
    op.execute("DROP FUNCTION IF EXISTS acquisition_band_insert_guard()")
