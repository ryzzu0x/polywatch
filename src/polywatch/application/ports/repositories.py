"""Port persistensi. Implementasinya ada di infrastructure/db."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from polywatch.domain.market import Market, MarketStatus, Timeframe
from polywatch.domain.scoring import WalletStats
from polywatch.domain.trade import Trade


class MarketRepository(Protocol):
    async def upsert(self, market: Market) -> int: ...

    async def get_by_slug(self, slug: str) -> Market | None: ...

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]: ...


class TradeRepository(Protocol):
    async def insert_many(self, trades: Sequence[Trade]) -> int:
        """Sisipkan idempoten; kembalikan jumlah baris baru."""
        ...

    async def last_traded_at(self, market_id: int) -> datetime | None: ...

    async def wallets_for_market(self, market_id: int) -> Sequence[str]: ...


class WalletStatsRepository(Protocol):
    async def upsert(self, stats: WalletStats) -> None: ...

    async def top(self, limit: int) -> Sequence[WalletStats]: ...

    async def get(self, wallet: str) -> WalletStats | None: ...


class WatchConfigRepository(Protocol):
    async def is_enabled(self, user_id: int, asset: str, timeframe: Timeframe) -> bool: ...

    async def set_enabled(
        self, user_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> None: ...


class AlertSettingsRepository(Protocol):
    async def get(self, user_id: int) -> dict[str, object]: ...

    async def save(self, user_id: int, values: dict[str, object]) -> None: ...
