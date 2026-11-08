"""Unit test scheduler siklus penuh (T-018 wiring) — interval milidetik.

Kontrak siklus (ADR-011/T-025): **callable coroutine**, bukan objek
ber-`.execute()`. Test di sini memakai fake berupa fungsi biasa;
test kontrak lewat container nyata ada di
`tests/integration/test_scheduler_contract.py`.

Membuktikan: (a) tiap siklus dipanggil oleh loop masing-masing, (b)
exception satu siklus dilaporkan tanpa menghentikan loop, (c) shutdown
membatalkan semua task rapi, (d) result baru (resolved) memicu
recompute untuk market itu.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from polywatch.main.config import Settings
from polywatch.main.scheduler import (
    cancel_all,
    create_scheduler_tasks,
    intervals_from_settings,
)

_TEST_ENV = {
    "BOT_TOKEN": "t",
    "OWNER_CHAT_ID": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
}


class FakeMonitor:
    """Penerima laporan siklus palsu (T-019)."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, bool]] = []

    async def record_cycle(self, kind: str, ok: bool, duration_s: float | None = None) -> None:
        self.reports.append((kind, ok))


@dataclass(frozen=True, slots=True)
class FakeResultSummary:
    market_id: int
    status: str


class _FakeCycles:
    """Pabrik callable siklus palsu (kontrak T-025: callable, bukan objek)."""

    def __init__(
        self,
        discover_raises: set[int] | None = None,
        result_cycles: list[Sequence[FakeResultSummary] | None] | None = None,
    ) -> None:
        self.discover_calls = 0
        self.result_calls = 0
        self.recompute_ids: list[int] = []
        self._discover_raises = discover_raises or set()
        self._result_cycles = result_cycles or []

    async def discover(self) -> object:
        self.discover_calls += 1
        if self.discover_calls in self._discover_raises:
            raise RuntimeError("discover boom")
        return object()

    async def record_result(self) -> list[FakeResultSummary]:
        index = self.result_calls
        self.result_calls += 1
        cycle = self._result_cycles[index] if index < len(self._result_cycles) else []
        if cycle is None:
            raise RuntimeError("record boom")
        return list(cycle)

    async def recompute(self, market_id: int) -> object:
        self.recompute_ids.append(market_id)
        return object()


def _settings(discover_s: float, result_s: float) -> Settings:
    env = {
        **_TEST_ENV,
        "DISCOVER_INTERVAL_S": str(discover_s),
        "RESULT_INTERVAL_S": str(result_s),
    }
    return Settings.model_validate(env)


async def _run_for(
    fake: _FakeCycles,
    duration_s: float,
    settings: Settings,
) -> list[asyncio.Task[None]]:
    """Jalankan scheduler sejenak lalu stop rapi; return task-nya (sudah selesai)."""
    stop = asyncio.Event()
    tasks = create_scheduler_tasks(
        fake.discover, fake.record_result, fake.recompute, settings, stop
    )
    await asyncio.sleep(duration_s)
    stop.set()
    await cancel_all(tasks)
    return tasks


async def test_interval_dari_settings() -> None:
    settings = _settings(discover_s=12.5, result_s=7.25)
    assert intervals_from_settings(settings) == (12.5, 7.25)


async def test_setiap_use_case_dipanggil() -> None:
    fake = _FakeCycles(result_cycles=[[FakeResultSummary(3, "resolved")]])
    await _run_for(fake, duration_s=0.15, settings=_settings(0.05, 0.05))
    assert fake.discover_calls >= 2
    assert fake.result_calls >= 2
    assert 3 in fake.recompute_ids  # market resolved direcompute


async def test_exception_tidak_menghentikan_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeCycles(
        discover_raises={1, 2},
        result_cycles=[None, None],  # dua siklus pertama gagal
    )
    with caplog.at_level(logging.ERROR, logger="polywatch.scheduler"):
        await _run_for(fake, duration_s=0.2, settings=_settings(0.03, 0.03))
    # Loop tetap hidup melewati siklus gagal.
    assert fake.discover_calls > 2
    assert fake.result_calls > 2
    # Error tercatat terstruktur.
    errors = [r for r in caplog.records if r.getMessage() == "siklus scheduler gagal"]
    assert len(errors) >= 4
    for record_log in errors:
        assert getattr(record_log, "event", None) == "scheduler_cycle_error"
        assert getattr(record_log, "cycle", None) in {"discover", "record_result"}


async def test_shutdown_membatalkan_semua_task() -> None:
    fake = _FakeCycles()
    stop = asyncio.Event()
    tasks = create_scheduler_tasks(
        fake.discover, fake.record_result, fake.recompute, _settings(0.05, 0.05), stop
    )
    names = {t.get_name() for t in tasks}
    assert names == {"scheduler-discover", "scheduler-result"}
    stop.set()
    await cancel_all(tasks)
    assert all(t.done() for t in tasks)
    # cancel_all idempoten untuk task yang sudah selesai.
    await cancel_all(tasks)
    assert all(t.done() for t in tasks)


async def test_market_timeout_tidak_direcompute() -> None:
    fake = _FakeCycles(result_cycles=[[FakeResultSummary(9, "timeout")]])
    await _run_for(fake, duration_s=0.1, settings=_settings(0.03, 0.03))
    assert 9 not in fake.recompute_ids


async def test_siklus_dilaporkan_ke_health_monitor() -> None:
    """T-019: tiap siklus (sukses & gagal) dilaporkan ke monitor."""
    fake = _FakeCycles(discover_raises={1})  # siklus kedua gagal
    monitor = FakeMonitor()
    stop = asyncio.Event()
    tasks = create_scheduler_tasks(
        fake.discover,
        fake.record_result,
        fake.recompute,
        _settings(0.03, 0.03),
        stop,
        monitor=monitor,
    )
    await asyncio.sleep(0.15)
    stop.set()
    await cancel_all(tasks)

    discover_reports = [ok for kind, ok in monitor.reports if kind == "discover"]
    result_reports = [ok for kind, ok in monitor.reports if kind == "result"]
    assert len(discover_reports) >= 3
    assert len(result_reports) >= 3
    assert False in discover_reports  # siklus gagal dilaporkan ok=False
    assert all(result_reports)  # record sukses semua
    # Hanya discover & result yang dilaporkan (recompute bukan komponen monitor).
    assert {kind for kind, _ in monitor.reports} == {"discover", "result"}


async def test_loop_tanpa_monitor_tetap_jalan() -> None:
    """Monitor tidak terpasang (None) tidak mengganggu loop."""
    fake = _FakeCycles()
    await _run_for(fake, duration_s=0.1, settings=_settings(0.03, 0.03))
    assert fake.discover_calls >= 2
    assert fake.result_calls >= 2
