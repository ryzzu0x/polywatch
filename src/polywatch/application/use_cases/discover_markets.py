"""Use case discover_markets: deteksi market Up/Down berbasis slug per jendela (T-024).

Deterministik: untuk tiap (aset, timeframe) dihitung window start =
`floor(now / durasi) * durasi`, lalu market dicari lewat `GET /markets?slug=<slug>`
untuk jendela berjalan + 2 ke depan (live/upcoming) dan 3 ke belakang dengan
`closed=true` (market live hilang dari pencarian slug setelah end — T-005).

Jalur cadangan list-endpoint hanya supplement dan wajib terfilter anti-stale:
slug harus cocok pola updown DAN `endDate` ±beberapa jam dari sekarang.
Aset & timeframe di-inject (AssetSource + default Settings + Timeframe domain);
pembentukan slug & validasi jendela ada di domain — adapter hanya eksekusi HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from polywatch.application.dto.market import MarketSnapshot
from polywatch.application.ports import Clock, MarketRepository, MarketSource
from polywatch.application.use_cases.market_catalog import AssetSource
from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import Market, MarketSlug, Timeframe, status_at

logger = logging.getLogger(__name__)

#: Jendela ke depan yang di-query (berjalan + 2 berikutnya).
DEFAULT_AHEAD_WINDOWS = 2
#: Jendela ke belakang yang di-query dengan `closed=true` (untuk record_result).
DEFAULT_PAST_WINDOWS = 3
#: `endDate` di luar ±jam ini dari sekarang dianggap market basi (anti-stale).
DEFAULT_MAX_END_AGE_HOURS = 6.0


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    """Ringkasan satu siklus discovery untuk log/monitoring."""

    new_markets: int
    total_markets: int
    skipped_slugs: int
    stale_filtered: int


class SlugSource(Protocol):
    """Sumber pencarian market per slug — bagian dari port MarketSource."""

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None: ...


async def _no_sleep(_: float) -> None:
    """No-op sleep untuk test/inject."""


@dataclass(slots=True)
class DiscoverMarkets:
    """Sinkronkan market yang sedang/telah tayang ke repository."""

    source: SlugSource
    repository: MarketRepository
    clock: Clock
    #: Aset yang sudah dikenal (tabel assets); digabung dengan `default_assets`.
    known_assets: AssetSource | None = None
    #: Aset default dari Settings (MARKET_ASSETS) — di-inject, bukan hardcode.
    default_assets: tuple[str, ...] = ("btc",)
    timeframes: tuple[Timeframe, ...] = (
        Timeframe.FIVE_MINUTES,
        Timeframe.FIFTEEN_MINUTES,
    )
    ahead_windows: int = DEFAULT_AHEAD_WINDOWS
    past_windows: int = DEFAULT_PAST_WINDOWS
    max_end_age_hours: float = DEFAULT_MAX_END_AGE_HOURS
    fetch_delay_s: float = 0.25
    #: Sleep antar-request — di-inject test (no-op) untuk jalan instan.
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Sumber cadangan list-endpoint (optional) — supplement, hasil wajib
    #: lolos filter anti-stale (T-024). Biasanya objek MarketSource yang sama.
    supplement_source: MarketSource | None = None

    async def execute(self) -> DiscoverySummary:
        now = self.clock.now()
        assets = await self._assets()
        saved: dict[str, MarketSnapshot] = {}
        new_markets = 0
        total_markets = 0
        skipped_slugs = 0
        stale_filtered = 0

        for asset in assets:
            for timeframe in self.timeframes:
                for offset in range(-self.past_windows, self.ahead_windows + 1):
                    slug = self._slug_for(asset, timeframe, now, offset)
                    snapshot = await self.source.fetch_by_slug(slug, closed=offset < 0)
                    if snapshot is None:
                        continue  # jendela tanpa market — normal
                    if self._stale_slug(slug, now) is None:
                        stale_filtered += 1
                        logger.warning(
                            "market basi di-skip",
                            extra={"event": "skipped_stale", "slug": slug},
                        )
                        continue
                    is_new, is_ok = await self._save(slug, snapshot, now)
                    if is_ok:
                        saved[slug] = snapshot
                        total_markets += 1
                        new_markets += int(is_new)
                    else:
                        skipped_slugs += 1
                    await self.sleep(self.fetch_delay_s)

        # Supplement (opsional): list-endpoint. Hasilnya wajib lolos filter
        # anti-stale — slug cocok pola updown DAN endDate ±max_end_age_hours.
        if self.supplement_source is not None:
            for timeframe in self.timeframes:
                for snapshot in await self.supplement_source.list_markets(timeframe):
                    parsed = self._stale_slug(snapshot.slug, now)
                    if parsed is None:
                        stale_filtered += 1
                        logger.warning(
                            "market basi/aneh dari list-endpoint di-skip",
                            extra={"event": "skipped_stale", "slug": snapshot.slug},
                        )
                        continue
                    if snapshot.slug in saved or await self.repository.get_by_slug(snapshot.slug):
                        continue  # sudah ditangani jalur slug
                    is_new, is_ok = await self._save(snapshot.slug, snapshot, now)
                    if is_ok:
                        total_markets += 1
                        new_markets += int(is_new)
                    else:
                        skipped_slugs += 1

        summary = DiscoverySummary(
            new_markets=new_markets,
            total_markets=total_markets,
            skipped_slugs=skipped_slugs,
            stale_filtered=stale_filtered,
        )
        logger.info(
            "discover_markets selesai",
            extra={
                "new_markets": summary.new_markets,
                "total_markets": summary.total_markets,
                "skipped_slugs": summary.skipped_slugs,
                "stale_filtered": summary.stale_filtered,
            },
        )
        return summary

    async def _save(self, slug: str, snapshot: MarketSnapshot, now: datetime) -> tuple[bool, bool]:
        """Upsert satu market.

        Return (baru?, tersimpan?) — tersimpan False berarti slug rusak
        (di-skip tanpa menggagalkan batch).
        """
        parsed = self._stale_slug(slug, now)
        if parsed is None:
            return False, False
        existing = await self.repository.get_by_slug(slug)
        await self.repository.upsert(
            Market(
                condition_id=snapshot.condition_id,
                slug=parsed,
                token_up=snapshot.token_up,
                token_down=snapshot.token_down,
                status=status_at(parsed, now),
            )
        )
        return existing is None, True

    async def _assets(self) -> tuple[str, ...]:
        known = await self.known_assets.list_symbols() if self.known_assets else []
        combined = {a.strip().lower() for a in (*known, *self.default_assets) if a.strip()}
        return tuple(sorted(combined))

    def _slug_for(self, asset: str, timeframe: Timeframe, now: datetime, offset: int) -> str:
        return build_slug(asset, timeframe, window_start(now, timeframe, offset))

    def _stale_slug(self, slug: str, now: datetime) -> MarketSlug | None:
        """Parse slug + validasi ke-segarian endDate.

        Return parsed slug bila valid dan `endDate` dalam ±`max_end_age_hours`
        dari sekarang; None berarti slug rusak ATAU market basi (anti-stale,
        T-024 — slug valid dengan endDate 11 bulan lalu tetap ditolak).
        """
        try:
            parsed = MarketSlug.parse(slug)
        except InvalidMarketSlugError:
            logger.warning("slug tidak cocok pola updown, di-skip", extra={"slug": slug})
            return None
        if abs(now - parsed.end_at) > timedelta(hours=self.max_end_age_hours):
            return None
        return parsed


def window_start(now: datetime, timeframe: Timeframe, offset: int = 0) -> datetime:
    """Start jendela berjalan = `floor(now/durasi)*durasi`, plus `offset` jendela.

    Tepat di batas (now == kelipatan durasi) jendela berjalan dimulai di
    `now` itu sendiri, sesuai konvensi `[start, end)`.
    """
    seconds = int(now.timestamp())
    duration = timeframe.seconds
    floored = (seconds // duration) * duration
    return datetime.fromtimestamp(floored + offset * duration, tz=UTC)


def build_slug(asset: str, timeframe: Timeframe, start: datetime) -> str:
    """Slug `{aset}-updown-{tf}-{unix_start}` — identitas market di Gamma."""
    return f"{asset}-updown-{timeframe.value}-{int(start.timestamp())}"


__all__ = [
    "DEFAULT_AHEAD_WINDOWS",
    "DEFAULT_MAX_END_AGE_HOURS",
    "DEFAULT_PAST_WINDOWS",
    "DiscoverMarkets",
    "DiscoverySummary",
    "SlugSource",
    "_no_sleep",
    "build_slug",
    "window_start",
]
