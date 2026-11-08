"""Port sumber data eksternal: market, trade, dan harga."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from polywatch.application.dto.market import (
    MarketResultSnapshot,
    MarketSnapshot,
    TokenMarketInfo,
)
from polywatch.application.dto.trade import FilledOrderSnapshot, TradeEventSnapshot, TradeSnapshot
from polywatch.domain.market import Timeframe


class MarketSource(Protocol):
    """Sumber daftar market dan statusnya (Polymarket Gamma API)."""

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        """Market tunggal lewat slug (T-024); None bila tidak ditemukan.

        `closed=True` untuk jendela yang sudah selesai.
        """
        ...

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]: ...

    async def get_result(self, slug: str) -> MarketResultSnapshot | None: ...


class TradeSource(Protocol):
    """Sumber trade per market (Polymarket Data API)."""

    async def fetch_trades(
        self, condition_id: str, since: datetime | None = None
    ) -> Sequence[TradeSnapshot]: ...


class TradeEventStream(Protocol):
    """Stream event trade real-time (CLOB WS market channel, T-011)."""

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        """Yield event `last_trade_price` untuk token yang di-subscribe."""
        ...


class LiveTokenSource(Protocol):
    """Sumber daftar token market aktif untuk ingest (T-026/T-028 refresh).

    Implementasi nyata (`SqlAlchemyLiveTokenQuery`) memilih market berbasis
    JENDELA WAKTU (ADR-015): `start_at <= now < end_at` dan status bukan
    RESOLVED — status `LIVE` bisa basi sehingga tidak lagi dipakai sebagai
    kriteria. Semantik watch default-enabled (ADR-012) tetap: pasangan
    tanpa baris override `enabled=false` dianggap aktif.
    """

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        """Token Up/Down market aktif dalam jendela waktunya, urut end_at."""
        ...


class ReceiptFetcher(Protocol):
    """Fetch + decode receipt on-chain untuk satu tx_hash (RPC Polygon)."""

    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot] | None:
        """Daftar fill di tx; None bila receipt belum tersedia (timeout)."""
        ...


class PriceSource(Protocol):
    """Sumber harga open/close untuk market yang sudah selesai."""

    name: str

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None: ...
