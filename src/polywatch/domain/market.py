"""Market Up/Down Polymarket beserta identitas dan waktunya."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from polywatch.domain.errors import (
    InvalidAssetError,
    InvalidMarketSlugError,
    InvalidMarketStatusTransitionError,
    InvalidOutcomePricesError,
    InvalidResolutionStatusError,
)

_MARKET_SLUG_PATTERN = re.compile(
    r"^(?P<asset>[a-z0-9]{2,10})-updown-(?P<timeframe>5m|15m)-(?P<start>\d{10})$"
)

_TIMEFRAME_SECONDS: dict[Timeframe, int] = {}


class Timeframe(StrEnum):
    """Durasi market Up/Down yang dipantau."""

    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @property
    def duration(self) -> timedelta:
        return timedelta(seconds=self.seconds)


_TIMEFRAME_SECONDS.update(
    {
        Timeframe.FIVE_MINUTES: 300,
        Timeframe.FIFTEEN_MINUTES: 900,
    }
)


class Outcome(StrEnum):
    """Sisi market biner."""

    UP = "up"
    DOWN = "down"


class MarketStatus(StrEnum):
    """Siklus hidup market."""

    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSED = "closed"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Asset:
    """Aset dasar (BTC, ETH, ...). Disimpan lowercase tanpa spasi."""

    symbol: str

    def __post_init__(self) -> None:
        normalized = self.symbol.strip().lower()
        if not normalized or not normalized.isalnum():
            raise InvalidAssetError(self.symbol)
        object.__setattr__(self, "symbol", normalized)


@dataclass(frozen=True, slots=True)
class MarketSlug:
    """Hasil parsing slug `{aset}-updown-{timeframe}-{unix_start}`."""

    asset: Asset
    timeframe: Timeframe
    start_at: datetime

    @classmethod
    def parse(cls, raw: str) -> MarketSlug:
        match = _MARKET_SLUG_PATTERN.match(raw.strip().lower())
        if match is None:
            raise InvalidMarketSlugError(raw)
        return cls(
            asset=Asset(match.group("asset")),
            timeframe=Timeframe(match.group("timeframe")),
            start_at=datetime.fromtimestamp(int(match.group("start")), tz=UTC),
        )

    @property
    def end_at(self) -> datetime:
        return self.start_at + self.timeframe.duration

    def is_live_at(self, moment: datetime) -> bool:
        return self.start_at <= moment < self.end_at


@dataclass(frozen=True, slots=True)
class Market:
    """Market yang tersimpan di database."""

    condition_id: str
    slug: MarketSlug
    token_up: str
    token_down: str
    status: MarketStatus


def outcome_from_prices(open_price: Decimal, close_price: Decimal) -> Outcome:
    """Aturan resolusi Up/Down: close >= open berarti UP."""
    return Outcome.UP if close_price >= open_price else Outcome.DOWN


_ALLOWED_TRANSITIONS: dict[MarketStatus, frozenset[MarketStatus]] = {
    MarketStatus.UPCOMING: frozenset({MarketStatus.LIVE, MarketStatus.CLOSED}),
    MarketStatus.LIVE: frozenset({MarketStatus.CLOSED}),
    MarketStatus.CLOSED: frozenset({MarketStatus.RESOLVED}),
    MarketStatus.RESOLVED: frozenset(),
}


def can_transition(current: MarketStatus, target: MarketStatus) -> bool:
    """Apakah transisi status market legal (hanya maju, tanpa melompati RESOLVED)."""
    return target in _ALLOWED_TRANSITIONS[current]


def transition_status(current: MarketStatus, target: MarketStatus) -> MarketStatus:
    """Validasi transisi status market; tolak transisi mundur/lompat dengan error domain."""
    if not can_transition(current, target):
        raise InvalidMarketStatusTransitionError(current.value, target.value)
    return target


def status_at(slug: MarketSlug, moment: datetime) -> MarketStatus:
    """Status market murni dari waktu: sebelum start = UPCOMING, saat berjalan =
    LIVE, setelah end = CLOSED (RESOLVED hanya lewat resolusi resmi Gamma)."""
    if moment < slug.start_at:
        return MarketStatus.UPCOMING
    if moment < slug.end_at:
        return MarketStatus.LIVE
    return MarketStatus.CLOSED


@dataclass(frozen=True, slots=True)
class MarketResult:
    """Hasil resmi market yang sudah resolved (sumber: Gamma).

    T-030/ADR-017: `open_price`/`close_price` opsional — bila semua sumber
    harga gagal, hasil resmi tetap disimpan dengan `price_source='unavailable'`
    dan harga NULL (arah hasil tetap dari resolusi resmi Gamma).
    """

    market_id: int
    open_price: Decimal | None
    close_price: Decimal | None
    result: Outcome
    volume_usdc: Decimal
    price_source: str
    resolved_at: datetime

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MarketResult):
            return NotImplemented
        return (
            self.market_id == other.market_id
            and self.open_price == other.open_price
            and self.close_price == other.close_price
            and self.result is other.result
            and self.volume_usdc == other.volume_usdc
            and self.price_source == other.price_source
            and self.resolved_at == other.resolved_at
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.market_id,
                self.open_price,
                self.close_price,
                self.result,
                self.volume_usdc,
                self.price_source,
                self.resolved_at,
            )
        )


def outcome_from_outcome_prices(raw: str) -> Outcome:
    """Peta `outcomePrices` Gamma (indeks 0 = Up) ke Outcome.

    Resolusi final berupa `"[\"1\", \"0\"]"` (Up menang) atau `"[\"0\", \"1\"]"`.
    `Decimal` dipakai supaya `"1.0"` / `"1.00"` tetap valid.
    """
    try:
        prices = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidOutcomePricesError(raw) from exc
    if not isinstance(prices, list) or len(prices) != 2:
        raise InvalidOutcomePricesError(raw)
    try:
        up, down = Decimal(str(prices[0])), Decimal(str(prices[1]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidOutcomePricesError(raw) from exc
    if (up, down) == (Decimal("1"), Decimal("0")):
        return Outcome.UP
    if (up, down) == (Decimal("0"), Decimal("1")):
        return Outcome.DOWN
    raise InvalidOutcomePricesError(raw)


def validate_uma_resolution_status(raw: str | None) -> bool:
    """Validasi `umaResolutionStatus` Gamma: `\"resolved\"` = sudah resolved.

    None (field hilang) dianggap belum resolved; nilai tak dikenal dinaikkan
    sebagai error domain supaya format API berubah terdeteksi, bukan diam.
    """
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized == "resolved":
        return True
    if normalized in {"", "pending"}:
        return False
    raise InvalidResolutionStatusError(raw)
