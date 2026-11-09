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
from polywatch.main.instance_lock import InstanceLock, InstanceLockError
from polywatch.main.logging import configure_logging
from polywatch.main.runner import run

logger = logging.getLogger("polywatch")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="polywatch", description="polywatch")
    parser.add_argument("--check", action="store_true", help="validasi konfigurasi lalu keluar")
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=None,
        help="interval heartbeat dalam detik (default: HEARTBEAT_INTERVAL_S/60)",
    )
    parser.add_argument("--version", action="version", version=f"polywatch {__version__}")
    sub = parser.add_subparsers(dest="command")
    recompute_all = sub.add_parser(
        "recompute-all",
        help="hitung ulang skor SEMUA wallet kandidat (backfill, T-032)",
    )
    recompute_all.add_argument(
        "--limit",
        type=int,
        default=None,
        help="batasi jumlah wallet yang diproses (urut wallet)",
    )
    recompute_all.add_argument(
        "--dry-run",
        action="store_true",
        help="hitung tanpa menulis ke wallet_stats",
    )
    return parser.parse_args(argv)


def _heartbeat_interval(args: argparse.Namespace, settings: Settings) -> float:
    if args.heartbeat_interval is not None:
        return float(args.heartbeat_interval)
    return max(float(settings.heartbeat_interval_s), 0.0)


async def _run_recompute_all(settings: Settings, limit: int | None, dry_run: bool) -> int:
    """Jalankan backfill `recompute_all` satu kali lalu keluar (T-032).

    Merakit RecomputeScores langsung di session factory (bukan scoped
    session task) — CLI ini bukan bagian runner, jadi session dikelola
    lokal di sini. Bot/scheduler/ingest tidak disentuh.
    """
    from polywatch.application.use_cases.recompute_scores import RecomputeScores
    from polywatch.infrastructure.db.repositories.trade_repository import (
        SqlAlchemyTradeRepository,
    )
    from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
        SqlAlchemyWalletStatsRepository,
    )

    container = Container.build(settings)
    try:
        async with container.session_factory() as session:
            use_case = RecomputeScores(
                trades=SqlAlchemyTradeRepository(session),
                wallet_stats=SqlAlchemyWalletStatsRepository(session),
            )
            summary = await use_case.recompute_all(limit=limit, dry_run=dry_run)
            if not dry_run:
                await session.commit()
    finally:
        await container.aclose()

    mode = " (dry-run)" if summary.dry_run else ""
    sys.stdout.write(
        f"recompute-all{mode}: {summary.wallets_processed} wallet diproses, "
        f"{summary.wallets_scored} dengan skor > 0, "
        f"skor maksimum {summary.top_score}"
        f" (wallet {summary.top_wallet or '-'}) "
        f"dalam {summary.duration_sec:.3f}s\n"
    )
    return 0


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

    if getattr(args, "command", None) == "recompute-all":
        # Subcommand operasional satu-kali (T-032): tidak mengambil instance
        # lock dan tidak menjalankan bot/scheduler/ingest.
        configure_logging(settings.log_level)
        return asyncio.run(_run_recompute_all(settings, limit=args.limit, dry_run=args.dry_run))

    configure_logging(settings.log_level)
    lock = InstanceLock(settings.lock_file)
    try:
        lock.acquire()
    except InstanceLockError:
        # Instance lain berjalan — keluar tanpa menyentuh Telegram/DB.
        return 3
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
        lock.release()
        return 0
    heartbeat = _heartbeat_interval(args, settings)
    try:
        asyncio.run(_run_with_bot(container, heartbeat_interval_sec=heartbeat))
    finally:
        lock.release()
    return 0


async def _run_with_bot(container: Container, heartbeat_interval_sec: float) -> None:
    """Rakit use case bot + scheduler lalu jalankan runner (tidak menyentuh
    Telegram saat --check)."""
    try:
        await container.setup_bot()
    except Exception:
        # Bot gagal dirakit (mis. jaringan) tidak boleh menghentikan service;
        # runner tetap jalan dengan heartbeat + scheduler saja.
        logger.exception(
            "bot gagal dirakit; runner jalan tanpa bot", extra={"event": "bot_setup_failed"}
        )
    try:
        # T-027: health monitor dirakit LEBIH DULU — scheduler dan ingest
        # melaporkan record_cycle/heartbeat ke objek yang sudah ada
        # (sebelumnya setup_scheduler duluan → monitor=None → record_cycle
        # tidak pernah dipanggil → alarm palsu "belum ada aktivitas").
        container.setup_health()
        container.setup_scheduler()
        container.setup_ingest()
    except Exception:
        # Health/scheduler/ingest gagal dirakit tidak boleh menghentikan
        # service; siklus yang gagal dilaporkan ke health monitor bila
        # terpasang.
        logger.exception(
            "health/scheduler/ingest gagal dirakit; runner jalan tanpa siklus",
            extra={"event": "scheduler_setup_failed"},
        )
    await run(container, heartbeat_interval_sec=heartbeat_interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
