"""Port persistensi. Implementasinya ada di infrastructure/db."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from polywatch.application.dto.market import (
    MarketResultEntry,
    PendingMarket,
    TokenMarketInfo,
)
from polywatch.application.dto.settings import WatchEntry
from polywatch.application.dto.trade import ResolvedTrade
from polywatch.domain.market import Market, MarketResult, MarketStatus, Timeframe
from polywatch.domain.scoring import WalletStats
from polywatch.domain.trade import Trade


class MarketRepository(Protocol):
    async def upsert(self, market: Market) -> int: ...

    async def get_by_slug(self, slug: str) -> Market | None: ...

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]: ...

    async def get_result(self, market_id: int) -> MarketResult | None: ...

    async def save_result(self, result: MarketResult) -> None: ...

    async def update_status(self, market_id: int, status: MarketStatus) -> None: ...

    async def has_result(self, market_id: int) -> bool: ...

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        """Token Up/Down + condition id satu market (untuk ingest/backfill)."""
        ...

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        """Market CLOSED tanpa market_results, siap diproses record_result."""
        ...


class MarketStatsRepository(Protocol):
    """Bacaan ringan untuk perintah bot /status dan /results (T-014/T-015).

    Dipisah dari `MarketRepository` (interface segregation): use case bot
    hanya butuh bacaan ini, dan fake test tidak perlu mengimplementasikan
    seluruh port persistensi.
    """

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]: ...

    async def count_by_status(self, status: MarketStatus) -> int:
        """Jumlah market berstatus tertentu (untuk /status)."""
        ...

    async def latest_results(
        self, asset: str, timeframe: Timeframe, limit: int
    ) -> Sequence[MarketResultEntry]:
        """Hasil market terbaru satu aset x timeframe (untuk /results)."""
        ...


class TradeStatsRepository(Protocol):
    """Bacaan jumlah trade untuk /status (T-014)."""

    async def count_since(self, since: datetime) -> int:
        """Jumlah trade sejak waktu tertentu (untuk /status)."""
        ...


class HealthActivityRepository(Protocol):
    """Bacaan aktivitas untuk health monitor (T-019).

    Interface segregation: HealthMonitor hanya butuh satu bacaan ringan,
    tidak seluruh port persistensi trade.
    """

    async def last_trade_at_live(self) -> datetime | None:
        """Waktu trade terakhir pada market berstatus LIVE (None bila kosong)."""
        ...


class UserRepository(Protocol):
    """Resolusi `users` dari telegram_id (MVP single-owner)."""

    async def get_id_by_telegram(self, telegram_id: int) -> int | None: ...

    async def get_or_create_by_telegram(self, telegram_id: int) -> int: ...


class TradeRepository(Protocol):
    async def insert_many(self, trades: Sequence[Trade]) -> int:
        """Sisipkan idempoten; kembalikan jumlah baris baru."""
        ...

    async def last_traded_at(self, market_id: int) -> datetime | None: ...

    async def wallets_for_market(self, market_id: int) -> Sequence[str]: ...

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        """Semua trade wallet pada market yang sudah resolve + arah hasilnya."""
        ...


class WalletStatsRepository(Protocol):
    async def upsert(self, stats: WalletStats) -> None: ...

    async def top(self, limit: int) -> Sequence[WalletStats]: ...

    async def get(self, wallet: str) -> WalletStats | None: ...


class WatchConfigRepository(Protocol):
    async def is_enabled(self, user_id: int, asset: str, timeframe: Timeframe) -> bool: ...

    async def set_enabled(
        self, user_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> None: ...

    async def list_all(self, user_id: int) -> Sequence[WatchEntry]: ...

    async def asset_exists(self, asset: str) -> bool: ...


class AlertSettingsRepository(Protocol):
    async def get(self, user_id: int) -> dict[str, object]: ...

    async def save(self, user_id: int, values: dict[str, object]) -> None: ...


class AlertsSentRepository(Protocol):
    """Riwayat alert terkirim (cooldown + audit delay_ms), T-017."""

    async def record(
        self,
        user_id: int,
        wallet: str,
        market_id: int,
        tx_hash: str,
        log_index: int,
        sent_at: datetime,
        delay_ms: int,
    ) -> None: ...

    async def last_sent_at(self, user_id: int, wallet: str) -> datetime | None: ...
