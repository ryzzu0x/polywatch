"""Runner aplikasi: loop heartbeat + shutdown rapi.

Heartbeat memakai port `Clock` untuk menghitung uptime, bukan
`datetime.now()` langsung. Interval dinyatakan dalam detik; test
memakai nilai pecahan (mis. 0.01) supaya cepat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from types import FrameType

from polywatch import __version__
from polywatch.main.container import Container

logger = logging.getLogger("polywatch.runner")

DEFAULT_HEARTBEAT_INTERVAL_SEC = 60.0


async def _heartbeat_once(container: Container, started_at_monotonic: float) -> None:
    uptime = container.clock.now().timestamp() - started_at_monotonic
    logger.info(
        "heartbeat",
        extra={
            "event": "heartbeat",
            "uptime_sec": round(uptime, 3),
            "version": __version__,
        },
    )


async def run(
    container: Container,
    heartbeat_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Jalankan loop heartbeat sampai sinyal berhenti.

    - SIGINT/SIGTERM memicu shutdown rapi.
    - `stop_event` memungkinkan test membatalkan loop tanpa sinyal.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("sinyal diterima, shutdown", extra={"event": "shutdown", "signal": signum})
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _request_stop, sig, None)

    started_at = container.clock.now().timestamp()
    logger.info(
        "runner start",
        extra={
            "event": "runner_start",
            "version": __version__,
            "heartbeat_interval_sec": heartbeat_interval_sec,
        },
    )
    try:
        while not stop_event.is_set():
            await _heartbeat_once(container, started_at)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=heartbeat_interval_sec)
    finally:
        logger.info("runner stop", extra={"event": "runner_stop"})
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
        await container.aclose()
