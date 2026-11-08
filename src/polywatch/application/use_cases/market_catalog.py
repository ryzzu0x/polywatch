"""Sumber aset & timeframe untuk discovery market Up/Down (T-024).

Aset diambil dari dua sumber yang digabung (uni, urut alfabetis):
- aset yang sudah dikenal di tabel `assets` (port AssetSource — repository),
- daftar default dari `Settings` (MARKET_ASSETS, dipisah koma).

Timeframe tetap milik domain (`Timeframe`) — tidak di-hardcode di use case.
"""

from __future__ import annotations

from typing import Protocol

from polywatch.domain.market import Timeframe

_DEFAULT_MARKET_ASSETS: tuple[str, ...] = (
    "btc",
    "eth",
    "sol",
    "xrp",
    "doge",
    "bnb",
    "hype",
    "zec",
    "xag",
    "xau",
)

#: Semua timeframe Up/Down yang didukung domain (5m, 15m).
ALL_TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.FIVE_MINUTES,
    Timeframe.FIFTEEN_MINUTES,
)


def default_market_assets() -> tuple[str, ...]:
    """Daftar aset default discovery (MARKET_ASSETS)."""
    return _DEFAULT_MARKET_ASSETS


class AssetSource(Protocol):
    """Sumber aset yang sudah dikenal (implementasi: repository assets)."""

    async def list_symbols(self) -> list[str]: ...


__all__ = ["ALL_TIMEFRAMES", "AssetSource", "default_market_assets"]
