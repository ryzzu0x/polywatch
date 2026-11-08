"""Entrypoint aplikasi: `polywatch` atau `python -m polywatch.main.cli`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from pydantic import ValidationError

from polywatch import __version__
from polywatch.main.config import Settings
from polywatch.main.container import Container
from polywatch.main.logging import configure_logging
from polywatch.main.runner import DEFAULT_HEARTBEAT_INTERVAL_SEC, run

logger = logging.getLogger("polywatch")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="polywatch", description="polywatch")
    parser.add_argument("--check", action="store_true", help="validasi konfigurasi lalu keluar")
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL_SEC,
        help="interval heartbeat dalam detik (default: 60)",
    )
    parser.add_argument("--version", action="version", version=f"polywatch {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = (
            ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
            or "tidak diketahui"
        )
        sys.stderr.write(
            "Konfigurasi tidak valid. Periksa .env (lihat .env.example).\n"
            f"Field bermasalah: {missing}\n{exc}\n"
        )
        return 2

    configure_logging(settings.log_level)
    container = Container.build(settings)
    logger.info(
        "polywatch start",
        extra={
            "event": "app_start",
            "version": __version__,
            "owner_chat_id": container.settings.owner_chat_id,
            "clock": type(container.clock).__name__,
        },
    )
    if args.check:
        sys.stdout.write("konfigurasi valid\n")
        return 0
    asyncio.run(_run_with_bot(container, heartbeat_interval_sec=args.heartbeat_interval))
    return 0


async def _run_with_bot(container: Container, heartbeat_interval_sec: float) -> None:
    """Rakit use case bot lalu jalankan runner (tidak menyentuh Telegram saat --check)."""
    try:
        await container.setup_bot()
    except Exception:
        # Bot gagal dirakit (mis. jaringan) tidak boleh menghentikan service;
        # runner tetap jalan dengan heartbeat saja.
        logger.exception(
            "bot gagal dirakit; runner jalan tanpa bot", extra={"event": "bot_setup_failed"}
        )
    await run(container, heartbeat_interval_sec=heartbeat_interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
