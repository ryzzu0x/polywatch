"""Penilaian wallet: edge dan ROI, bukan win rate mentah (ADR-003)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MIN_SAMPLE_MARKETS = 50
DEFAULT_MIN_SCORE = 60.0


@dataclass(frozen=True, slots=True)
class WalletStats:
    """Ringkasan performa wallet pada market yang sudah resolve."""

    wallet: str
    markets_count: int
    wins: int
    avg_entry: Decimal | None
    pnl: Decimal
    staked: Decimal
    is_market_maker: bool = False

    @property
    def win_rate(self) -> Decimal:
        if self.markets_count <= 0:
            return Decimal(0)
        return Decimal(self.wins) / Decimal(self.markets_count)

    @property
    def edge(self) -> Decimal:
        """Selisih win rate terhadap harga masuk rata-rata.

        Beli di 0.90 lalu menang 85% menghasilkan edge negatif.
        """
        if self.avg_entry is None or self.markets_count <= 0:
            return Decimal(0)
        return self.win_rate - self.avg_entry

    @property
    def roi(self) -> Decimal:
        if self.staked <= 0:
            return Decimal(0)
        return self.pnl / self.staked

    @property
    def has_enough_sample(self) -> bool:
        return self.markets_count >= MIN_SAMPLE_MARKETS

    @property
    def is_alertable(self) -> bool:
        return self.has_enough_sample and not self.is_market_maker
