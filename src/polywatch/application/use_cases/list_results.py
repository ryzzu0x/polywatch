"""Use case list_results: hasil market terakhir untuk /results (T-015).

Validasi aset & timeframe lewat domain (`Asset`, `Timeframe`); data hasil
diambil lewat port `MarketRepository.latest_results` (bukan SQL di handler).
Output string Bahasa Indonesia, ringkas, jumlah baris dibatasi.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from polywatch.application.dto.market import MarketResultEntry
from polywatch.application.ports import MarketStatsRepository
from polywatch.domain.market import Asset, Timeframe

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


class ListResults:
    """Susun teks hasil market terakhir per aset x timeframe."""

    def __init__(self, markets: MarketStatsRepository) -> None:
        self._markets = markets

    async def execute(self, asset_raw: str, timeframe_raw: str, limit: int | None = None) -> str:
        """Validasi input lewat domain lalu format hasil.

        Raises:
            InvalidAssetError: aset tidak valid menurut domain.
            ValueError: timeframe tidak dikenal.
        """
        asset = Asset(asset_raw)  # raise InvalidAssetError
        timeframe = Timeframe(timeframe_raw.strip().lower())  # raise ValueError
        count = max(1, min(limit if limit is not None else DEFAULT_LIMIT, MAX_LIMIT))

        rows = await self._markets.latest_results(asset.symbol, timeframe, count)
        return self.format(asset.symbol, timeframe, rows)

    @staticmethod
    def format(asset: str, timeframe: Timeframe, rows: Sequence[MarketResultEntry]) -> str:
        """Formatter murni (dites unit tanpa DB/Telegram)."""
        if not rows:
            return f"Belum ada hasil untuk {asset.upper()} {timeframe.value}."
        lines = [f"Hasil {asset.upper()} {timeframe.value} (terbaru dulu):"]
        for i, row in enumerate(rows, start=1):
            hasil = "Up" if row.result.value == "up" else "Down"
            # T-030/ADR-017: harga open/close bisa NULL (price_source
            # 'unavailable' saat semua sumber harga gagal).
            open_txt = row.open_price if row.open_price is not None else "-"
            close_txt = row.close_price if row.close_price is not None else "-"
            lines.append(
                f"{i}. open {open_txt} -> close {close_txt} | {hasil} "
                f"| vol {row.volume_usdc} | {row.start_at:%Y-%m-%d %H:%M} UTC"
            )
        return "\n".join(lines)


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "ListResults", "MarketResultEntry"]
