"""Trade dan posisi per wallet per market."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from polywatch.domain.errors import InvalidWalletError
from polywatch.domain.market import Outcome

_WALLET_PATTERN = re.compile(r"0x[0-9a-f]{40}")


class Action(StrEnum):
    """Arah transaksi wallet di market."""

    BUY = "buy"
    SELL = "sell"


def normalize_wallet(address: str) -> str:
    """Validasi alamat EVM dan kembalikan bentuk lowercase `0x...`."""
    candidate = address.strip().lower()
    if _WALLET_PATTERN.fullmatch(candidate) is None:
        raise InvalidWalletError(address)
    return candidate


@dataclass(frozen=True, slots=True)
class Trade:
    """Satu transaksi. Kunci idempotensi: (tx_hash, log_index, wallet)."""

    tx_hash: str
    log_index: int
    market_id: int
    wallet: str
    outcome: Outcome
    action: Action
    price: Decimal
    size: Decimal
    usdc: Decimal
    traded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "wallet", normalize_wallet(self.wallet))
        if self.price < 0 or self.price > 1:
            raise ValueError(f"harga outcome harus di antara 0 dan 1, dapat {self.price}")
        if self.size < 0 or self.usdc < 0:
            raise ValueError("size dan usdc tidak boleh negatif")


@dataclass(frozen=True, slots=True)
class Position:
    """Posisi bersih satu wallet di satu market."""

    wallet: str
    market_id: int
    net_up: Decimal = Decimal(0)
    net_down: Decimal = Decimal(0)
    cost_up: Decimal = Decimal(0)
    cost_down: Decimal = Decimal(0)

    @property
    def is_two_sided(self) -> bool:
        """Memegang kedua sisi sekaligus — ciri market maker."""
        return self.net_up > 0 and self.net_down > 0
