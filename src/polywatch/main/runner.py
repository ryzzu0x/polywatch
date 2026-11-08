"""Runner aplikasi: bot long polling + scheduler + loop heartbeat + shutdown rapi.

Satu proses menjalankan bersamaan:
(a) long polling bot Telegram (bila `setup_bot` berhasil),
(b) scheduler siklus penuh (discover, record_result, recompute_scores —
    bila `setup_scheduler` dipanggil),
(c) siklus heartbeat existing,
(d) hook ingest trade bila terpasang (`ingest_hooks`) tanpa mengubah
    perilaku bila kosong,
(e) pemeriksaan kesehatan berkala (T-019, no-op bila `health_monitor`
    belum dirakit).

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
from polywatch.main.scheduler import cancel_all, create_scheduler_tasks

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


async def _run_ingest_hooks(container: Container) -> None:
    """Jalankan hook ingest sebagai task kontinyu (T-026).

    Hook ingest sekarang adalah loop kontinyu (langganan WS hidup seumur
    proses, refresh token tiap siklus) — dipanggil sekali dan dibiarkan
    berjalan sampai dibatalkan saat shutdown. Exception di dalam hook
    ditangkap di `IngestTrades.run` (stream restart); error di luar itu
    di-log dan loop dimulai ulang setelah jeda singkat supaya proses
    tidak kehilangan ingest hanya karena satu kegagalan.
    """
    if not container.ingest_hooks:
        return
    while True:
        for hook in list(container.ingest_hooks):
            try:
                await hook()
            except Exception:
                logger.exception(
                    "hook ingest gagal; restart setelah jeda",
                    extra={"event": "ingest_hook_error"},
                )
                await asyncio.sleep(5.0)


def _start_scheduler(container: Container, stop_event: asyncio.Event) -> list[asyncio.Task[None]]:
    """Buat task scheduler bila use case siklus sudah dirakit; kosong bila belum.

    Health monitor (bila dirakit) menerima laporan tiap siklus (T-019)."""
    discover = getattr(container, "discover_cycle", None)
    record_result = getattr(container, "record_result_cycle", None)
    recompute = getattr(container, "recompute_cycle", None)
    if discover is None or record_result is None or recompute is None:
        return []
    return create_scheduler_tasks(
        discover=discover,
        record_result=record_result,
        recompute=recompute,
        settings=container.settings,
        stop_event=stop_event,
        monitor=getattr(container, "health_monitor", None),
    )


async def _poll_bot(container: Container) -> None:
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


async def _health_loop(
    container: Container, interval_sec: float, stop_event: asyncio.Event
) -> None:
    """Loop pemeriksaan kesehatan (T-019) — no-op bila monitor tidak terpasang."""
    monitor = getattr(container, "health_monitor", None)
    if monitor is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        while not stop_event.is_set():
            try:
                report = await monitor.check()
                if report.stale_names:
                    logger.warning(
                        "komponen basi terdeteksi",
                        extra={
                            "event": "health_stale_detected",
                            "components": ",".join(report.stale_names),
                        },
                    )
            except Exception:
                logger.exception(
                    "pemeriksaan kesehatan gagal", extra={"event": "health_check_error"}
                )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)


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
        # (b) scheduler siklus penuh (no-op bila use case belum dirakit)
        scheduler_tasks = _start_scheduler(container, stop_event)
        # (c) heartbeat existing
        tasks.append(
            asyncio.create_task(
                _heartbeat_loop(container, started_at, heartbeat_interval_sec, stop_event)
            )
        )
        # (d) hook ingest kontinyu (no-op bila kosong)
        tasks.append(asyncio.create_task(_run_ingest_hooks(container)))
        # (e) pemeriksaan kesehatan berkala (no-op bila monitor tidak terpasang)
        health_interval = float(getattr(container.settings, "health_check_interval_s", 30.0))
        tasks.append(
            asyncio.create_task(
                _health_loop(container, max(health_interval, 0.0), stop_event),
                name="health-monitor",
            )
        )

        await stop_event.wait()
    finally:
        logger.info("runner stop", extra={"event": "runner_stop"})
        await cancel_all(scheduler_tasks)
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
