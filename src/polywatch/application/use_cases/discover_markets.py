"""Use case discover_markets: deteksi market Up/Down berbasis slug per jendela (T-024).

Deterministik: untuk tiap (aset, timeframe) dihitung window start =
`floor(now / durasi) * durasi`, lalu market dicari lewat `GET /markets?slug=<slug>`
untuk jendela berjalan + 2 ke depan (live/upcoming) dan 3 ke belakang dengan
`closed=true` (market live hilang dari pencarian slug setelah end — T-005).

Jalur cadangan list-endpoint (supplement) tidak lagi dipakai di jalur normal
(T-028/ADR-010): sumber kebenaran adalah slug per jendela; supplement hanya
opt-in eksplisit (parameter `supplement_source`) dan hasilnya tetap wajib
terfilter anti-stale: slug harus cocok pola updown DAN `endDate` ±beberapa
jam dari sekarang.
Aset & timeframe di-inject (AssetSource + default Settings + Timeframe domain);
pembentukan slug & validasi jendela ada di domain — adapter hanya eksekusi HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


#: T-029/ADR-016: default konkurensi request Gamma per siklus discover.
DEFAULT_MAX_CONCURRENCY = 5
#: T-029/ADR-016: TTL cache negatif aset (aset tanpa market sama sekali,
#: mis. xag/xau — tidak di-query ulang selama TTL ini).
DEFAULT_ASSET_TTL_S = 600.0


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    """Ringkasan satu siklus discovery untuk log/monitoring."""

    new_markets: int
    total_markets: int
    skipped_slugs: int
    stale_filtered: int
    duration_ms: int
    dead_asset_skips: int


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
    #: T-029: sleep antar-request TIDAK lagi dipakai di jalur paralel —
    #: pacing ≤5 req/s dijaga adapter Gamma (`_throttle` async, dipanggil
    #: per request). Field tetap untuk kompatibilitas jalur supplement.
    fetch_delay_s: float = 0.0
    #: T-029: konkurensi maksimum request Gamma per siklus (semaphore).
    #: Pacing per-request tetap dijaga adapter Gamma (≤5 req/s).
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: T-029: TTL cache negatif aset — aset tanpa satu pun market tidak
    #: di-query ulang sampai TTL habis.
    dead_asset_ttl_s: float = DEFAULT_ASSET_TTL_S
    #: Clock monoton untuk cache negatif (di-inject test).
    monotonic: Callable[[], float] = time.monotonic
    #: Cache negatif aset: symbol → waktu monoton penandaan mati.
    _dead_until: dict[str, float] = field(default_factory=dict, repr=False)
    #: Sleep antar-request — di-inject test (no-op) untuk jalan instan.
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Sumber cadangan list-endpoint — OPT-IN, default None (T-028/ADR-010:
    #: jalur normal tidak memakainya; hanya menghasilkan ribuan payload
    #: rusak + request sia-sia). Bila diisi, hasilnya wajib lolos filter
    #: anti-stale.
    supplement_source: MarketSource | None = None

    async def execute(self) -> DiscoverySummary:
        started = self.monotonic()
        now = self.clock.now()
        assets = await self._assets()
        saved: dict[str, MarketSnapshot] = {}
        new_markets = 0
        total_markets = 0
        skipped_slugs = 0
        stale_filtered = 0
        dead_asset_skips = 0

        # T-029: kandidat slug dihitung dulu (skip aset mati via cache
        # negatif), lalu semua fetch dijalankan paralel dengan semaphore.
        candidates: list[tuple[str, bool]] = []  # (slug, closed)
        active_assets: list[str] = []
        for asset in assets:
            if self._is_dead(asset):
                dead_asset_skips += 1
                logger.debug(
                    "aset tanpa market di-skip (cache negatif)",
                    extra={"event": "discover_dead_asset_skip", "asset": asset},
                )
                continue
            active_assets.append(asset)
            for timeframe in self.timeframes:
                for offset in range(-self.past_windows, self.ahead_windows + 1):
                    slug = self._slug_for(asset, timeframe, now, offset)
                    candidates.append((slug, offset < 0))

        semaphore = asyncio.Semaphore(max(self.max_concurrency, 1))

        async def _fetch(slug: str, closed: bool) -> tuple[str, MarketSnapshot | None]:
            async with semaphore:
                snapshot = await self.source.fetch_by_slug(slug, closed=closed)
            await self.sleep(self.fetch_delay_s)
            return slug, snapshot

        fetched = await asyncio.gather(*(_fetch(slug, closed) for slug, closed in candidates))

        for slug, snapshot in fetched:
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

        # T-029: cache negatif — aset aktif yang tidak menemukan satu pun
        # market ditandai mati untuk TTL; aset yang menemukan market
        # dihapus dari cache (positif kembali langsung di-query).
        found_assets = {MarketSlug.parse(slug).asset.symbol for slug in saved}
        now_mono = self.monotonic()
        for asset in active_assets:
            if asset in found_assets:
                self._dead_until.pop(asset, None)
            else:
                self._dead_until[asset] = now_mono + self.dead_asset_ttl_s

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
            duration_ms=round((self.monotonic() - started) * 1000),
            dead_asset_skips=dead_asset_skips,
        )
        logger.info(
            "discover_markets selesai",
            extra={
                "new_markets": summary.new_markets,
                "total_markets": summary.total_markets,
                "skipped_slugs": summary.skipped_slugs,
                "stale_filtered": summary.stale_filtered,
                "duration_ms": summary.duration_ms,
                "dead_asset_skips": summary.dead_asset_skips,
            },
        )
        return summary

    def _is_dead(self, asset: str) -> bool:
        """Aset dalam masa cache negatif (T-029) — tanpa market pada siklus
        terakhir dan TTL belum habis."""
        until = self._dead_until.get(asset)
        return until is not None and self.monotonic() < until

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
    "DEFAULT_ASSET_TTL_S",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_END_AGE_HOURS",
    "DEFAULT_PAST_WINDOWS",
    "DiscoverMarkets",
    "DiscoverySummary",
    "SlugSource",
    "_no_sleep",
    "build_slug",
    "window_start",
]
