"""Unit test runner heartbeat + health loop (T-003, T-019) — tanpa tidur panjang."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

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


class FakeHealthMonitor:
    """Monitor kesehatan palsu — mencatat setiap check()."""

    def __init__(self, fail: bool = False) -> None:
        self.checks = 0
        self._fail = fail

    async def check(self) -> Any:
        self.checks += 1
        if self._fail:
            raise RuntimeError("check boom")
        return None

    async def record_cycle(self, kind: str, ok: bool) -> None:  # pragma: no cover
        return None


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


class HealthContainer(FakeContainer):
    """Container + health_monitor palsu untuk test loop kesehatan."""

    def __init__(self, clock: FakeClock, monitor: FakeHealthMonitor) -> None:
        super().__init__(clock)
        self.health_monitor = monitor
        self.settings = Settings.model_validate({**_TEST_ENV, "HEALTH_CHECK_INTERVAL_S": "0.01"})


async def _run_until_stopped(container: Container, delay_sec: float = 0.05) -> None:
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(delay_sec)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stopper())
        await run(container, heartbeat_interval_sec=0.01, stop_event=stop)


async def _run_health_until_stopped(container: Container, delay_sec: float = 0.15) -> None:
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(delay_sec)
        stop.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stopper())
        await run(container, heartbeat_interval_sec=0.2, stop_event=stop)


def test_health_loop_memanggil_check_berkala() -> None:
    monitor = FakeHealthMonitor()
    container = HealthContainer(FakeClock(), monitor)
    asyncio.run(_run_health_until_stopped(container))
    assert monitor.checks >= 2  # pemeriksaan berkala (interval 10 ms)


def test_health_loop_tanpa_monitor_no_op() -> None:
    container = FakeContainer(FakeClock())
    asyncio.run(_run_health_until_stopped(container))  # tidak boleh error
    assert container.closed is True


def test_health_loop_check_gagal_tidak_menghentikan_runner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = FakeHealthMonitor(fail=True)
    container = HealthContainer(FakeClock(), monitor)
    with caplog.at_level(logging.ERROR, logger="polywatch.runner"):
        asyncio.run(_run_health_until_stopped(container))
    assert monitor.checks >= 2  # loop tetap hidup setelah gagal
    assert any(r.getMessage() == "pemeriksaan kesehatan gagal" for r in caplog.records)
    assert container.closed is True


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
