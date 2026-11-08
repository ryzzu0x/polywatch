"""Unit test formatter logging JSON (T-003)."""

from __future__ import annotations

import json
import logging

from pydantic import SecretStr

from polywatch.main.logging import JsonFormatter


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="polywatch.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_menghasilkan_json_satu_baris_valid() -> None:
    line = JsonFormatter().format(_record("halo", event="heartbeat", uptime_sec=1.5))
    parsed = json.loads(line)
    assert "\n" not in line
    assert parsed["msg"] == "halo"
    assert parsed["level"] == "INFO"
    assert parsed["event"] == "heartbeat"
    assert parsed["uptime_sec"] == 1.5
    assert parsed["logger"] == "polywatch.test"
    assert "ts" in parsed


def test_formatter_meredam_extra_field_sensitif() -> None:
    line = JsonFormatter().format(
        _record(
            "konteks",
            bot_token="123456:supersecret",
            password="hunter2",
            api_key="abcd",
            dsn="postgresql+asyncpg://u:secret@h/db",
        )
    )
    assert "supersecret" not in line
    assert "hunter2" not in line
    assert "postgresql+asyncpg" not in line
    parsed = json.loads(line)
    assert parsed["bot_token"] == "********"
    assert parsed["password"] == "********"
    assert parsed["api_key"] == "********"
    assert parsed["dsn"] == "********"


def test_formatter_meredam_secretstr() -> None:
    line = JsonFormatter().format(_record("konteks", token=SecretStr("123456:supersecret")))
    assert "supersecret" not in line
    assert json.loads(line)["token"] == "********"


def test_formatter_tanpa_exc_info_tetap_json_valid() -> None:
    line = JsonFormatter().format(_record("aman"))
    assert json.loads(line)["msg"] == "aman"
