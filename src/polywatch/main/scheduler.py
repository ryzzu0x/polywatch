"""Scheduler siklus penuh: discover → record_result → recompute_scores.

Task periodik asyncio dengan interval dari `Settings`:
- `discover_interval_s` (default 30): sinkronkan market live/upcoming,
- `result_interval_s` (default 30): untuk market CLOSED yang belum punya
  hasil → `record_result`, lalu setelah result baru tersimpan →
  `recompute_scores` untuk market itu.

Satu siklus gagal (exception) tidak mematikan proses: ditangkap, di-log
error terstruktur (`scheduler_cycle_error`), siklus berikutnya tetap jalan.

T-019: setiap siklus dilaporkan ke health monitor (`record_cycle`) —
keberhasilan dihitung sebagai aktivitas terbaru komponen, kegagalan tidak
(memicu notifikasi basi via `HealthMonitor.check`). Laporan yang gagal
dikirim tidak mengganggu loop.

Semua task dibatalkan rapi lewat `stop_event` (SIGINT/SIGTERM lewat runner).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Protocol

from polywatch import __version__
from polywatch.application.use_cases.health_monitor import CycleKind
from polywatch.main.config import Settings

logger = logging.getLogger("polywatch.scheduler")

DEFAULT_DISCOVER_INTERVAL_SEC = 30.0
DEFAULT_RESULT_INTERVAL_SEC = 30.0


class DiscoverCycle(Protocol):
    """Use case discover (adapter protocol — bisa di-inject fake di test)."""

    async def execute(self) -> Any: ...


class ResultCycle(Protocol):
    """Use case record_result; return daftar ringkasan hasil."""

    async def execute(self) -> list[Any]: ...


class RecomputeCycle(Protocol):
    """Use case recompute_scores untuk satu market."""

    async def execute(self, market_id: int) -> Any: ...


class HealthReporter(Protocol):
    """Penerima laporan siklus scheduler (mis. HealthMonitor, T-019)."""

    async def record_cycle(self, kind: CycleKind, ok: bool) -> None: ...


def _resolved_ids(summaries: list[Any]) -> list[int]:
    """Ambil market_id resolved dari ringkasan RecordResult (duck-typed)."""
    ids: list[int] = []
    for summary in summaries:
        market_id = getattr(summary, "market_id", None)
        status = getattr(summary, "status", None)
        if isinstance(market_id, int) and status == "resolved":
            ids.append(market_id)
    return ids


async def _run_cycle(name: str, cycle: Any) -> tuple[bool, Any]:
    """Jalankan satu siklus; return (sukses, hasil). Exception di-log, tidak diteruskan."""
    try:
        return True, await cycle()
    except Exception:
        logger.exception(
            "siklus scheduler gagal",
            extra={"event": "scheduler_cycle_error", "cycle": name},
        )
        return False, None


async def _report(monitor: HealthReporter | None, kind: CycleKind, ok: bool) -> None:
    """Laporkan hasil siklus ke health monitor; gagal lapor tidak mengganggu loop."""
    if monitor is None:
        return
    try:
        await monitor.record_cycle(kind, ok)
    except Exception:
        logger.exception(
            "laporan siklus ke health monitor gagal",
            extra={"event": "health_report_error", "cycle": kind},
        )


async def _wait(stop_event: asyncio.Event, interval_sec: float) -> None:
    """Tidur sampai interval habis atau stop_event di-set (shutdown rapi)."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)


async def discover_loop(
    discover: DiscoverCycle,
    interval_sec: float,
    stop_event: asyncio.Event,
    monitor: HealthReporter | None = None,
) -> None:
    """Loop discover market: jalan segera lalu tiap interval."""
    while not stop_event.is_set():
        ok, _ = await _run_cycle("discover", discover.execute)
        await _report(monitor, "discover", ok)
        await _wait(stop_event, interval_sec)
    logger.info(
        "loop discover berhenti", extra={"event": "scheduler_loop_stop", "cycle": "discover"}
    )


async def result_loop(
    record_result: ResultCycle,
    recompute: RecomputeCycle,
    interval_sec: float,
    stop_event: asyncio.Event,
    monitor: HealthReporter | None = None,
) -> None:
    """Loop result: resolve market CLOSED baru, lalu recompute market itu."""
    while not stop_event.is_set():
        ok, summaries = await _run_cycle("record_result", record_result.execute)
        await _report(monitor, "result", ok)
        for market_id in _resolved_ids(summaries or []):
            await _run_cycle("recompute_scores", lambda mid=market_id: recompute.execute(mid))
        await _wait(stop_event, interval_sec)
    logger.info("loop result berhenti", extra={"event": "scheduler_loop_stop", "cycle": "result"})


def intervals_from_settings(settings: Settings) -> tuple[float, float]:
    """Interval (discover, result) dari Settings dengan default aman."""
    discover = float(getattr(settings, "discover_interval_s", DEFAULT_DISCOVER_INTERVAL_SEC))
    result = float(getattr(settings, "result_interval_s", DEFAULT_RESULT_INTERVAL_SEC))
    return max(discover, 0.0), max(result, 0.0)


def create_scheduler_tasks(
    discover: DiscoverCycle,
    record_result: ResultCycle,
    recompute: RecomputeCycle,
    settings: Settings,
    stop_event: asyncio.Event,
    monitor: HealthReporter | None = None,
) -> list[asyncio.Task[None]]:
    """Buat kedua task loop scheduler (dibatalkan bersama stop_event)."""
    discover_sec, result_sec = intervals_from_settings(settings)
    logger.info(
        "scheduler start",
        extra={
            "event": "scheduler_start",
            "version": __version__,
            "discover_interval_s": discover_sec,
            "result_interval_s": result_sec,
            "health_monitor": monitor is not None,
        },
    )
    return [
        asyncio.create_task(
            discover_loop(discover, discover_sec, stop_event, monitor),
            name="scheduler-discover",
        ),
        asyncio.create_task(
            result_loop(record_result, recompute, result_sec, stop_event, monitor),
            name="scheduler-result",
        ),
    ]


async def cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    """Batalkan semua task scheduler rapi (idempoten untuk task selesai)."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "DEFAULT_DISCOVER_INTERVAL_SEC",
    "DEFAULT_RESULT_INTERVAL_SEC",
    "DiscoverCycle",
    "HealthReporter",
    "RecomputeCycle",
    "ResultCycle",
    "cancel_all",
    "create_scheduler_tasks",
    "discover_loop",
    "intervals_from_settings",
    "result_loop",
]
