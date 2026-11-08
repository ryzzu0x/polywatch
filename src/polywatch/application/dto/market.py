"""DTO market dan hasil market."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polywatch.domain.market import MarketStatus, Outcome


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
    """Data resolusi market.

    `outcome` terisi hanya bila resolusi resmi sudah keluar (`resolved=True`);
    asalnya dari `outcomePrices` Gamma (indeks 0 = Up). Resolusi resmi adalah
    sumber kebenaran arah hasil — harga hanya informasi.
    """

    slug: str
    open_price: Decimal | None
    close_price: Decimal | None
    volume_usdc: Decimal
    resolved: bool
    outcome: Outcome | None = None


@dataclass(frozen=True, slots=True)
class PendingMarket:
    """Market berstatus CLOSED yang belum punya baris market_results.

    Dibuat repository; diproses use case record_result.
    """

    market_id: int
    condition_id: str
    slug: str
    status: MarketStatus


@dataclass(frozen=True, slots=True)
class TokenMarketInfo:
    """Identitas market yang dibutuhkan ingest trade (T-011)."""

    market_id: int
    condition_id: str
    token_up: str
    token_down: str
    slug: str
