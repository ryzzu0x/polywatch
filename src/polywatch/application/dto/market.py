"""DTO market dan hasil market."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Data market apa adanya dari sumber eksternal."""

    slug: str
    condition_id: str
    token_up: str
    token_down: str
    volume_usdc: Decimal
    closed: bool


@dataclass(frozen=True, slots=True)
class MarketResultSnapshot:
    """Data resolusi market."""

    slug: str
    open_price: Decimal | None
    close_price: Decimal | None
    volume_usdc: Decimal
    resolved: bool
