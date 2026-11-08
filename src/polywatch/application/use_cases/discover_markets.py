"""Use case discover_markets: deteksi market Up/Down baru dari sumber Gamma.

Pure application layer: hanya bergantung pada domain + port. Aset & timeframe
dideteksi otomatis dari slug (`MarketSlug.parse`); slug yang tidak cocok
di-skip tanpa menggagalkan batch. Upsert idempoten lewat port MarketRepository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from polywatch.application.dto.market import MarketSnapshot
from polywatch.application.ports import Clock, MarketRepository, MarketSource
from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import Market, MarketSlug, Timeframe, status_at

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    """Ringkasan satu siklus discovery untuk log/monitoring."""

    new_markets: int
    total_markets: int
    skipped_slugs: int


@dataclass(slots=True)
class DiscoverMarkets:
    """Sinkronkan market yang sedang/telah tayang ke repository."""

    source: MarketSource
    repository: MarketRepository
    clock: Clock
    timeframes: tuple[Timeframe, ...] = field(
        default=(Timeframe.FIVE_MINUTES, Timeframe.FIFTEEN_MINUTES)
    )

    async def execute(self) -> DiscoverySummary:
        new_markets = 0
        total_markets = 0
        skipped_slugs = 0

        for timeframe in self.timeframes:
            snapshots: list[MarketSnapshot] = list(await self.source.list_markets(timeframe))
            for snapshot in snapshots:
                slug = self._parse_slug(snapshot)
                if slug is None:
                    skipped_slugs += 1
                    continue

                status = status_at(slug, self.clock.now())
                market = Market(
                    condition_id=snapshot.condition_id,
                    slug=slug,
                    token_up=snapshot.token_up,
                    token_down=snapshot.token_down,
                    status=status,
                )
                existing = await self.repository.get_by_slug(snapshot.slug)
                await self.repository.upsert(market)
                total_markets += 1
                if existing is None:
                    new_markets += 1

        summary = DiscoverySummary(
            new_markets=new_markets,
            total_markets=total_markets,
            skipped_slugs=skipped_slugs,
        )
        logger.info(
            "discover_markets selesai",
            extra={
                "new_markets": summary.new_markets,
                "total_markets": summary.total_markets,
                "skipped_slugs": summary.skipped_slugs,
            },
        )
        return summary

    def _parse_slug(self, snapshot: MarketSnapshot) -> MarketSlug | None:
        """Parse slug; None berarti tidak cocok pola Up/Down (di-skip, di-log)."""
        try:
            return MarketSlug.parse(snapshot.slug)
        except InvalidMarketSlugError:
            logger.warning(
                "slug tidak cocok pola updown, di-skip",
                extra={"slug": snapshot.slug, "condition_id": snapshot.condition_id},
            )
            return None


__all__ = ["DiscoverMarkets", "DiscoverySummary"]
