"""Trade dan posisi per wallet per market."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from polywatch.domain.errors import InvalidWalletError, OversellError
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


def apply_trade(position: Position, trade: Trade) -> Position:
    """Terapkan satu trade ke posisi, kembalikan posisi baru.

    BUY menambah `net_*` dan `cost_*` (biaya akumulasi harga masuk). SELL
    mengurangi `net_*` saja — `cost_*` tetap menjadi dasar PnL posisi yang masih
    terbuka (average entry dihitung berbobot size, ADR-003). SELL melebihi
    posisi ditolak `OversellError`.
    """
    if (trade.wallet, trade.market_id) != (position.wallet, position.market_id):
        raise ValueError(
            "trade tidak cocok dengan posisi: "
            f"({trade.wallet}, {trade.market_id}) != ({position.wallet}, {position.market_id})"
        )
    net = position.net_up if trade.outcome is Outcome.UP else position.net_down
    if trade.action is Action.SELL and trade.size > net:
        raise OversellError(trade.wallet, trade.market_id, trade.outcome, trade.size, net)
    delta_net = trade.size if trade.action is Action.BUY else -trade.size
    delta_cost = trade.usdc if trade.action is Action.BUY else Decimal(0)
    if trade.outcome is Outcome.UP:
        return replace(
            position,
            net_up=position.net_up + delta_net,
            cost_up=position.cost_up + delta_cost,
        )
    return replace(
        position,
        net_down=position.net_down + delta_net,
        cost_down=position.cost_down + delta_cost,
    )


def positions_from_trades(trades: list[Trade]) -> dict[tuple[str, int], Position]:
    """Agregasi daftar trade menjadi posisi bersih per (wallet, market_id).

    Posisi dimulai nol; trade diproses berurutan (urutan list = urutan waktu).
    """
    positions: dict[tuple[str, int], Position] = {}
    for trade in trades:
        key = (trade.wallet, trade.market_id)
        base = positions.get(key) or Position(wallet=trade.wallet, market_id=trade.market_id)
        positions[key] = apply_trade(base, trade)
    return positions


def realize(position: Position, result: Outcome) -> Decimal:
    """PnL posisi bersih saat market resolve: sisi menang bernilai 1, kalah 0.

    Pendapatan = net sisi menang; biaya = total cost kedua sisi (posisi sisi
    kalah hangus). Bisa negatif (rugi).
    """
    winnings = position.net_up if result is Outcome.UP else position.net_down
    return winnings - position.cost_up - position.cost_down


def average_entry(position: Position) -> Decimal | None:
    """Harga masuk rata-rata berbobot size untuk sisi dengan posisi tersisa.

    Kembalikan None bila tidak ada posisi terbuka di kedua sisi. Bila dua sisi
    terbuka (market maker), dipakai sisi dengan net terbesar.
    """
    if position.net_up > 0 and position.net_up >= position.net_down:
        return position.cost_up / position.net_up
    if position.net_down > 0:
        return position.cost_down / position.net_down
    return None
