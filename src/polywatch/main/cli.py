"""Entrypoint aplikasi: `polywatch` atau `python -m polywatch.main.cli`."""

from __future__ import annotations

import argparse
import logging
import sys

from pydantic import ValidationError

from polywatch import __version__
from polywatch.main.config import Settings
from polywatch.main.container import Container
from polywatch.main.logging import configure_logging

logger = logging.getLogger("polywatch")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="polywatch", description="polywatch")
    parser.add_argument("--check", action="store_true", help="validasi konfigurasi lalu keluar")
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
            "version": __version__,
            "owner_chat_id": container.settings.owner_chat_id,
            "clock": type(container.clock).__name__,
        },
    )
    if args.check:
        sys.stdout.write("konfigurasi valid\n")
        return 0
    logger.info("fondasi siap; bot Telegram diimplementasikan pada T-014")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
