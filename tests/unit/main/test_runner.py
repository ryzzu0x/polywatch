"""Unit test runner heartbeat (T-003) — tanpa tidur panjang."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from polywatch.main.config import Settings
from polywatch.main.container import Container
from polywatch.main.runner import run

_TEST_ENV = {
    "BOT_TOKEN": "t",
    "OWNER_CHAT_ID": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
}


class FakeClock:
    """Jam palsu yang maju tiap dipanggil — deterministik."""

    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=UTC)
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        self._now += timedelta(seconds=1)
        return self._now


class FakeContainer(Container):
    """Container dengan clock palsu; tidak menyentuh DB."""

    def __init__(self, clock: FakeClock) -> None:
        self.settings = Settings.model_validate(_TEST_ENV)
        self.clock = clock
        self._engine = None
        self._session_factory = None
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def _run_until_stopped(container: Container, delay_sec: float = 0.05) -> None:
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(delay_sec)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stopper())
        await run(container, heartbeat_interval_sec=0.01, stop_event=stop)


def test_run_heartbeat_lalu_berhenti_rapi(caplog: pytest.LogCaptureFixture) -> None:
    container = FakeContainer(FakeClock())
    with caplog.at_level(logging.INFO, logger="polywatch.runner"):
        asyncio.run(_run_until_stopped(container))

    beats = [r for r in caplog.records if r.getMessage() == "heartbeat"]
    assert beats, "heartbeat harus tercatat minimal sekali"
    for record in beats:
        assert getattr(record, "uptime_sec", None) is not None
        assert getattr(record, "version", None)


def test_run_menutup_container_saat_berhenti() -> None:
    container = FakeContainer(FakeClock())
    asyncio.run(_run_until_stopped(container))
    assert container.closed is True


def test_heartbeat_jam_lewat_port_clock() -> None:
    clock = FakeClock()
    asyncio.run(_run_until_stopped(FakeContainer(clock)))
    assert clock.calls >= 1


def test_heartbeat_extra_field_json_serializable() -> None:
    container = FakeContainer(FakeClock())
    captured: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("polywatch.runner")
    logger.setLevel(logging.INFO)
    handler = Grab()
    logger.addHandler(handler)
    try:
        asyncio.run(_run_until_stopped(container))
    finally:
        logger.removeHandler(handler)
    beats = [r for r in captured if r.getMessage() == "heartbeat"]
    assert beats
    payload: dict[str, object] = {
        "msg": beats[0].getMessage(),
        "uptime_sec": getattr(beats[0], "uptime_sec", None),
        "version": getattr(beats[0], "version", None),
    }
    assert json.loads(json.dumps(payload))["msg"] == "heartbeat"
