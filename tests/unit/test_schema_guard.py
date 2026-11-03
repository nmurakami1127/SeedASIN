from db.schema_guard import MESSAGE, MESSAGE_0018, explain


class _Col(Exception):
    pass


def test_undefined_cycle_column_is_explained_and_other_errors_are_not():
    e = RuntimeError("wrapper")
    e.__cause__ = type("UndefinedColumn", (Exception,), {})('column "cycle_id" does not exist')
    assert explain(e) == MESSAGE
    assert explain(ValueError("something else")) is None
    assert explain(type("UndefinedColumn", (Exception,), {})('column "foo" does not exist')) is None


def test_undefined_end_reached_column_points_at_0018():
    e = RuntimeError("wrapper")
    e.__cause__ = type("UndefinedColumn", (Exception,), {})('column "end_reached" does not exist')
    assert explain(e) == MESSAGE_0018


def test_undefined_acquisition_band_table_points_at_0019():
    from db.schema_guard import MESSAGE_0019
    e = RuntimeError("wrapper")
    e.__cause__ = type("UndefinedTable", (Exception,), {})('relation "acquisition_band" does not exist')
    assert explain(e) == MESSAGE_0019
    assert explain(type("UndefinedTable", (Exception,), {})('relation "other_table" does not exist')) is None
