from db.schema_guard import MESSAGE, explain


class _Col(Exception):
    pass


def test_undefined_cycle_column_is_explained_and_other_errors_are_not():
    e = RuntimeError("wrapper")
    e.__cause__ = type("UndefinedColumn", (Exception,), {})('column "cycle_id" does not exist')
    assert explain(e) == MESSAGE
    assert explain(ValueError("something else")) is None
    assert explain(type("UndefinedColumn", (Exception,), {})('column "foo" does not exist')) is None
