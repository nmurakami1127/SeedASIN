import json

from applog.logger import StructuredLogger


def test_log_writes_one_json_line_with_module_marketplace_event(capsys):
    logger = StructuredLogger(module="M12", marketplace="US")
    logger.log("ADMIN_RETRY_REISSUE", operator="user-1", target_count=3)

    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0

    record = json.loads(out)
    assert record["module"] == "M12"
    assert record["marketplace"] == "US"
    assert record["event"] == "ADMIN_RETRY_REISSUE"
    assert record["operator"] == "user-1"
    assert record["target_count"] == 3
    assert "timestamp" in record


def test_log_without_marketplace_is_null():
    logger = StructuredLogger(module="M1")
    # コンストラクタ引数省略時、marketplaceはNoneのまま保持される。
    assert logger._marketplace is None
