"""Use case bot_status: teks ringkasan /status (T-014).

Menyusun status bot untuk owner: market live terakhir yang diketahui, jumlah
market per status, jumlah trade 24 jam terakhir, status WS/RPC terakhir bila
tersedia, dan versi aplikasi. Data diambil lewat port repository (bukan SQL
di handler); hasilnya string Bahasa Indonesia siap kirim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from polywatch import __version__
from polywatch.application.ports import (
    Clock,
    MarketStatsRepository,
    TradeStatsRepository,
)
from polywatch.domain.market import MarketStatus

logger = logging.getLogger(__name__)

_ALL_STATUSES = (
    MarketStatus.UPCOMING,
    MarketStatus.LIVE,
    MarketStatus.CLOSED,
    MarketStatus.RESOLVED,
)


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Data mentah status untuk keperluan log/test."""

    last_live: str | None
    counts_by_status: dict[str, int]
    trades_24h: int
    ws_status: str | None
    rpc_status: str | None


class BotStatus:
    """Susun teks /status dari repository + status stream (opsional)."""

    def __init__(
        self,
        markets: MarketStatsRepository,
        trades: TradeStatsRepository,
        clock: Clock,
        stream_statuses: list[str] | None = None,
    ) -> None:
        self._markets = markets
        self._trades = trades
        self._clock = clock
        self._stream_statuses = stream_statuses or []

    async def snapshot(self) -> StatusSnapshot:
        """Kumpulkan data status (dipisah agar mudah dites)."""
        live_markets = await self._markets.list_by_status(MarketStatus.LIVE)
        last_live = live_markets[-1].slug.asset.symbol if live_markets else None
        counts = {
            status.value: await self._markets.count_by_status(status) for status in _ALL_STATUSES
        }
        since = self._clock.now() - timedelta(hours=24)
        trades_24h = await self._trades.count_since(since)
        ws_status, rpc_status = _parse_stream_status(self._stream_statuses)
        return StatusSnapshot(
            last_live=last_live,
            counts_by_status=counts,
            trades_24h=trades_24h,
            ws_status=ws_status,
            rpc_status=rpc_status,
        )

    async def execute(self) -> str:
        """Teks /status siap kirim (Bahasa Indonesia, ringkas)."""
        snap = await self.snapshot()
        lines = ["Status polywatch:"]
        lines.append(f"Versi: {__version__}")
        lines.append(f"Market live terakhir: {snap.last_live or 'tidak ada'}")
        counts = " | ".join(f"{k}: {v}" for k, v in snap.counts_by_status.items())
        lines.append(f"Market per status: {counts}")
        lines.append(f"Trade 24 jam terakhir: {snap.trades_24h}")
        if snap.ws_status or snap.rpc_status:
            lines.append(f"Stream: ws={snap.ws_status or '-'} rpc={snap.rpc_status or '-'}")
        return "\n".join(lines)


def _parse_stream_status(statuses: list[str]) -> tuple[str | None, str | None]:
    """Parse status WS/RPC dari daftar entri `ws:connected` / `rpc:ok`.

    Entri terakhir menang (status paling baru).
    """
    ws = rpc = None
    for item in statuses:
        name, _, value = item.partition(":")
        if name == "ws":
            ws = value or None
        elif name == "rpc":
            rpc = value or None
    return ws, rpc


__all__ = ["BotStatus", "StatusSnapshot"]
