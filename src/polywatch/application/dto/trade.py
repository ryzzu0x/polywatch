"""DTO trade dari sumber eksternal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from polywatch.domain.market import Outcome
from polywatch.domain.trade import Action


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
