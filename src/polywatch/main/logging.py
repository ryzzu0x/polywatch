"""Logging JSON terstruktur — siap dibaca journald."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

_SECRET_KEY_PATTERN = re.compile(r"(token|secret|password|api_?key|dsn)$", re.IGNORECASE)
_REDACTED = "********"


def _redact(key: str, value: Any) -> Any:
    """Samarkan nilai kunci terindikasi secret (jangan sampai bocor ke log)."""
    if isinstance(value, SecretStr):
        return _REDACTED
    if isinstance(value, str) and _SECRET_KEY_PATTERN.search(key):
        return _REDACTED
    return value


_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Format satu baris JSON per record.

    Kunci ekstra terindikasi secret (`*token*`, `*secret*`, `*password*`,
    `*api_key*`, `*dsn*`) otomatis disamarkan.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Pasang handler JSON ke root logger (idempoten).

    T-027: httpx/httpcore di-redam ke WARNING — tiap request Gamma/RPC
    ditulis INFO oleh library dan membanjiri journald.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
