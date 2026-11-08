"""DTO trade dari sumber eksternal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from polywatch.domain.market import Outcome
from polywatch.domain.trade import Action, Trade

__all__ = ["FilledOrderSnapshot", "ResolvedTrade", "TradeEventSnapshot", "TradeSnapshot"]


@dataclass(frozen=True, slots=True)
class TradeEventSnapshot:
    """Event `last_trade_price` CLOB WS (T-011): pemicu fetch receipt."""

    tx_hash: str
    asset_id: str
    condition_id: str
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class FilledOrderSnapshot:
    """Satu fill on-chain (log OrderFilled ter-decode, T-005c/T-011)."""

    tx_hash: str
    log_index: int
    maker: str
    taker: str
    side: int  # 0 = BUY, 1 = SELL (enum Side kontrak exchange)
    token_id: int
    size: Decimal
    usdc: Decimal
    fee: int


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    """Satu trade mentah dari Data API, belum dipetakan ke id market lokal."""

    tx_hash: str
    log_index: int
    wallet: str
    outcome: Outcome
    action: Action
    price: Decimal
    size: Decimal
    usdc: Decimal
    traded_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedTrade:
    """Trade satu wallet pada market yang sudah resolve, beserta hasilnya.

    Dibentuk repository dengan join `trades` ↔ `market_results`; use case
    hanya mengonsumsi bentuk ini (tanpa SQL).
    """

    trade: Trade
    winning: Outcome
    last_trade_at: datetime | None = None
