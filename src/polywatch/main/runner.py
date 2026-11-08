"""Runner aplikasi: bot long polling + loop heartbeat + shutdown rapi.

Satu proses menjalankan bersamaan:
(a) long polling bot Telegram (bila `setup_bot` berhasil),
(b) siklus heartbeat existing,
(c) hook ingest trade bila terpasang (`ingest_hooks`) tanpa mengubah
    perilaku bila kosong.

SIGINT/SIGTERM membatalkan semua task lalu menutup container (session Bot +
engine DB) lewat `aclose`.
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


async def _run_ingest_hooks(container: Container, interval_sec: float) -> None:
    """Jalankan hook ingest (bila ada) tiap interval — tanpa mengubah
    perilaku runner bila tidak ada hook terpasang."""
    if not container.ingest_hooks:
        return
    while True:
        for hook in list(container.ingest_hooks):
            try:
                await hook()
            except Exception:
                logger.exception("hook ingest gagal", extra={"event": "ingest_hook_error"})
        await asyncio.sleep(interval_sec)


async def _poll_bot(container: Container) -> None:
    """Long polling bot sampai dibatalkan (session Bot ditutup aclose)."""
    from aiogram import Bot, Dispatcher

    bot = container._bot
    dispatcher = container._dispatcher
    assert isinstance(bot, Bot)
    assert isinstance(dispatcher, Dispatcher)
    me = await bot.get_me()
    logger.info(
        "bot getMe sukses",
        extra={"event": "bot_get_me", "bot_username": me.username},
    )
    with contextlib.suppress(asyncio.CancelledError):
        # handle_signals=False: sinyal ditangani runner (satu tempat).
        await dispatcher.start_polling(bot, handle_signals=False)


async def run(
    container: Container,
    heartbeat_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Jalankan bot + heartbeat + hook ingest sampai sinyal berhenti.

    - SIGINT/SIGTERM memicu shutdown rapi (semua task dibatalkan, container
      ditutup).
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
    tasks: list[asyncio.Task[None]] = []
    try:
        # (a) long polling bot — dipasang bila dispatcher tersedia
        if getattr(container, "_dispatcher", None) is not None:
            tasks.append(asyncio.create_task(_poll_bot(container), name="bot-polling"))
        # (b) heartbeat existing
        tasks.append(
            asyncio.create_task(
                _heartbeat_loop(container, started_at, heartbeat_interval_sec, stop_event)
            )
        )
        # (c) hook ingest (no-op bila kosong)
        tasks.append(asyncio.create_task(_run_ingest_hooks(container, heartbeat_interval_sec)))

        await stop_event.wait()
    finally:
        logger.info("runner stop", extra={"event": "runner_stop"})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
        await container.aclose()


async def _heartbeat_loop(
    container: Container,
    started_at: float,
    interval_sec: float,
    stop_event: asyncio.Event,
) -> None:
    """Loop heartbeat lama, dipecah agar bisa dibatalkan bersama bot."""
    with contextlib.suppress(asyncio.CancelledError):
        while not stop_event.is_set():
            await _heartbeat_once(container, started_at)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
