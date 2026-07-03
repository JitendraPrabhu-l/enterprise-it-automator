"""Structured JSON logging with per-request correlation IDs.

Replaces the plain-text logging.basicConfig() previously used across the
app. Structured logs are the prerequisite for making anything queryable in
a real log aggregator (Stage 3.4's observability work assumes this) — a
correlation ID threading through one request's log lines is what makes it
possible to reconstruct "everything that happened for ticket #42" from a
log stream, which plain unstructured text can't support without fragile
grep-and-hope.
"""

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
