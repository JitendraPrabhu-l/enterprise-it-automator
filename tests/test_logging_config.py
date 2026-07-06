"""Tests for structured JSON logging with correlation IDs (Stage 3.3)."""

import json
import logging

from app.logging_config import JsonFormatter, get_request_id, set_request_id


def _make_record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger", level=level, pathname="x.py", lineno=1,
        msg=message, args=(), exc_info=None,
    )


def test_json_formatter_produces_valid_json():
    formatter = JsonFormatter()
    record = _make_record("hello world")
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert "timestamp" in parsed


def test_json_formatter_includes_request_id_when_set():
    set_request_id("req-abc-123")
    formatter = JsonFormatter()
    record = _make_record("did a thing")
    parsed = json.loads(formatter.format(record))
    assert parsed["request_id"] == "req-abc-123"
    set_request_id(None)


def test_json_formatter_omits_request_id_when_unset():
    set_request_id(None)
    formatter = JsonFormatter()
    record = _make_record("no correlation")
    parsed = json.loads(formatter.format(record))
    assert "request_id" not in parsed


def test_get_and_set_request_id_roundtrip():
    set_request_id("xyz")
    assert get_request_id() == "xyz"
    set_request_id(None)
    assert get_request_id() is None


def test_json_formatter_includes_exception_traceback():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test.logger", level=logging.ERROR, pathname="x.py", lineno=1,
            msg="something failed", args=(), exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "exception" in parsed
    assert "ValueError: boom" in parsed["exception"]
