"""Market Up/Down Polymarket beserta identitas dan waktunya."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from polywatch.domain.errors import InvalidAssetError, InvalidMarketSlugError

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
