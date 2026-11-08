"""Unit test scheduler siklus penuh (T-018 wiring) — interval milidetik.

Membuktikan: (a) tiap use case dipanggil oleh loop masing-masing, (b)
exception satu siklus dilaporkan tanpa menghentikan loop, (c) shutdown
membatalkan semua task rapi, (d) result baru (resolved) memicu
recompute_scores untuk market itu.
"""

from __future__ import annotations

import asyncio
import logging
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


class FakeDiscover:
    def __init__(self, raise_times: set[int] | None = None) -> None:
        self.calls = 0
        self._raise_times = raise_times or set()

    async def execute(self) -> object:
        self.calls += 1
        if self.calls in self._raise_times:
            raise RuntimeError("discover boom")
        return object()


@dataclass(frozen=True, slots=True)
class FakeResultSummary:
    market_id: int
    status: str


class FakeRecordResult:
    """`None` di `summaries_per_cycle` berarti siklus itu raise."""

    def __init__(
        self, summaries_per_cycle: list[list[FakeResultSummary] | None] | None = None
    ) -> None:
        self.calls = 0
        self._per_cycle = summaries_per_cycle or []

    async def execute(self) -> list[FakeResultSummary]:
        index = self.calls
        self.calls += 1
        cycle = self._per_cycle[index] if index < len(self._per_cycle) else []
        if cycle is None:
            raise RuntimeError("record boom")
        return cycle


class FakeRecompute:
    def __init__(self) -> None:
        self.market_ids: list[int] = []

    async def execute(self, market_id: int) -> object:
        self.market_ids.append(market_id)
        return object()


def _settings(discover_s: float, result_s: float) -> Settings:
    env = {
        **_TEST_ENV,
        "DISCOVER_INTERVAL_S": str(discover_s),
        "RESULT_INTERVAL_S": str(result_s),
    }
    return Settings.model_validate(env)


async def _run_for(
    discover: FakeDiscover,
    record_result: FakeRecordResult,
    recompute: FakeRecompute,
    duration_s: float,
    settings: Settings,
) -> list[asyncio.Task[None]]:
    """Jalankan scheduler sejenak lalu stop rapi; return task-nya (sudah selesai)."""
    stop = asyncio.Event()
    tasks = create_scheduler_tasks(discover, record_result, recompute, settings, stop)
    await asyncio.sleep(duration_s)
    stop.set()
    await cancel_all(tasks)
    return tasks


async def test_interval_dari_settings() -> None:
    settings = _settings(discover_s=12.5, result_s=7.25)
    assert intervals_from_settings(settings) == (12.5, 7.25)


async def test_setiap_use_case_dipanggil() -> None:
    discover = FakeDiscover()
    record = FakeRecordResult(summaries_per_cycle=[[FakeResultSummary(3, "resolved")]])
    recompute = FakeRecompute()
    await _run_for(discover, record, recompute, duration_s=0.15, settings=_settings(0.05, 0.05))
    assert discover.calls >= 2
    assert record.calls >= 2
    assert 3 in recompute.market_ids  # market resolved direcompute


async def test_exception_tidak_menghentikan_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discover = FakeDiscover(raise_times={1, 2})  # dua siklus pertama gagal
    record = FakeRecordResult(summaries_per_cycle=[None, None])  # dua siklus gagal
    recompute = FakeRecompute()
    with caplog.at_level(logging.ERROR, logger="polywatch.scheduler"):
        await _run_for(discover, record, recompute, duration_s=0.2, settings=_settings(0.03, 0.03))
    # Loop tetap hidup melewati siklus gagal.
    assert discover.calls > 2
    assert record.calls > 2
    # Error tercatat terstruktur.
    errors = [r for r in caplog.records if r.getMessage() == "siklus scheduler gagal"]
    assert len(errors) >= 4
    for record_log in errors:
        assert getattr(record_log, "event", None) == "scheduler_cycle_error"
        assert getattr(record_log, "cycle", None) in {"discover", "record_result"}


async def test_shutdown_membatalkan_semua_task() -> None:
    discover = FakeDiscover()
    record = FakeRecordResult()
    recompute = FakeRecompute()
    stop = asyncio.Event()
    tasks = create_scheduler_tasks(discover, record, recompute, _settings(0.05, 0.05), stop)
    names = {t.get_name() for t in tasks}
    assert names == {"scheduler-discover", "scheduler-result"}
    stop.set()
    await cancel_all(tasks)
    assert all(t.done() for t in tasks)
    # cancel_all idempoten untuk task yang sudah selesai.
    await cancel_all(tasks)
    assert all(t.done() for t in tasks)


async def test_market_timeout_tidak_direcompute() -> None:
    discover = FakeDiscover()
    record = FakeRecordResult(summaries_per_cycle=[[FakeResultSummary(9, "timeout")]])
    recompute = FakeRecompute()
    await _run_for(discover, record, recompute, duration_s=0.1, settings=_settings(0.03, 0.03))
    assert 9 not in recompute.market_ids
