"""Port sumber data eksternal: market, trade, dan harga."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from polywatch.application.dto.market import MarketResultSnapshot, MarketSnapshot
from polywatch.application.dto.trade import TradeSnapshot
from polywatch.domain.market import Timeframe


class MarketSource(Protocol):
    """Sumber daftar market dan statusnya (Polymarket Gamma API)."""

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]: ...

    async def get_result(self, slug: str) -> MarketResultSnapshot | None: ...


class TradeSource(Protocol):
    """Sumber trade per market (Polymarket Data API)."""

    async def fetch_trades(
        self, condition_id: str, since: datetime | None = None
    ) -> Sequence[TradeSnapshot]: ...


class PriceSource(Protocol):
    """Sumber harga open/close untuk market yang sudah selesai."""

    name: str

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None: ...
