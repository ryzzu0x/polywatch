"""Use case sweep_market_status: selaraskan status market dengan waktu (T-028).

Sweep dua arah, memakai aturan domain `status_at` (status murni dari waktu):

- **Tutup:** market `end_at < now` dengan status UPCOMING/LIVE (status basi —
  discovery lambat atau resolusi tertunda) → transisi ke `CLOSED` lewat
  `transition_status` (UPCOMING→CLOSED dan LIVE→CLOSED keduanya legal).
  Ini yang membuat `record_result` (hanya memproses status CLOSED) bisa
  menemukan market selesai untuk di-resolve.
- **Buka:** market UPCOMING yang jendelanya sudah berjalan
  (`start_at <= now < end_at`) → transisi ke `LIVE`. Siklus discover penuh
  untuk banyak aset bisa memakan menit-an; tanpa sweep ini, market baru
  tersimpan UPCOMING dan tidak pernah LIVE sampai discover berikutnya
  selesai — query "market LIVE aktif" (mis. /status) kosong padahal
  ingest sudah jalan (ingest sendiri berbasis jendela waktu, ADR-015).

Sifat: batch, idempoten (market dengan status sudah sesuai waktu tidak
tersentuh), aman dijalankan berulang tiap siklus result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from polywatch.application.ports import Clock, MarketRepository
from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import (
    MarketSlug,
    MarketStatus,
    can_transition,
    status_at,
    transition_status,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """Hasil satu siklus sweep untuk log/monitoring."""

    closed: int
    started: int


class SweepMarketStatus:
    """Selaraskan status market UPCOMING/LIVE dengan jendela waktunya."""

    def __init__(self, repository: MarketRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    async def execute(self) -> SweepSummary:
        """Sweep kandidat; return jumlah yang ditutup dan dibuka."""
        now = self._clock.now()
        candidates = await self._repository.list_stale_open(now)
        closed = 0
        started = 0
        for item in candidates:
            try:
                expected = status_at(MarketSlug.parse(item.slug), now)
            except InvalidMarketSlugError:
                logger.error(
                    "sweep: slug market tidak valid, di-skip",
                    extra={"event": "market_sweep_bad_slug", "slug": item.slug},
                )
                continue
            if expected is item.status or not can_transition(item.status, expected):
                continue  # sudah sesuai / transisi tidak legal — tidak disentuh
            new_status = transition_status(item.status, expected)
            await self._repository.update_status(item.market_id, new_status)
            if new_status is MarketStatus.CLOSED:
                closed += 1
            else:
                started += 1
            logger.info(
                "status market di-sweep",
                extra={
                    "event": "market_sweep_transition",
                    "market_id": item.market_id,
                    "slug": item.slug,
                    "from_status": item.status.value,
                    "to_status": new_status.value,
                },
            )
        logger.info(
            "sweep status market selesai",
            extra={"event": "market_sweep_done", "closed": closed, "started": started},
        )
        return SweepSummary(closed=closed, started=started)


__all__ = ["SweepMarketStatus", "SweepSummary"]
