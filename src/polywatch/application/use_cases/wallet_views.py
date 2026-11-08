"""Use case wallet_views: /top dan /wallet (T-016).

`/top [n]` menampilkan wallet skor tertinggi (kecuali market maker) dari
`wallet_stats`; `/wallet <alamat>` statistik satu wallet + status watchlist.
Validasi alamat lewat `normalize_wallet` domain — format salah ditolak
dengan `InvalidWalletError` yang dipetakan handler ke pesan ramah.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

from polywatch.application.ports import UserRepository, WalletStatsRepository, WatchlistRepository
from polywatch.domain.errors import InvalidWalletError
from polywatch.domain.scoring import WalletStats
from polywatch.domain.trade import normalize_wallet

logger = logging.getLogger(__name__)

DEFAULT_TOP = 10
MAX_TOP = 25


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


class TopWallets:
    """/top [n]: wallet skor tertinggi dari wallet_stats."""

    def __init__(self, wallet_stats: WalletStatsRepository) -> None:
        self._wallet_stats = wallet_stats

    async def execute(self, limit: int | None = None) -> str:
        count = DEFAULT_TOP if limit is None else max(1, min(limit, MAX_TOP))
        rows = await self._wallet_stats.top(count)
        return self.format(rows)

    @staticmethod
    def format(rows: Sequence[WalletStats]) -> str:
        """Formatter murni (dites unit tanpa DB/Telegram)."""
        if not rows:
            return "Belum ada wallet dengan skor. Jalankan recompute setelah market resolve."
        lines = ["Wallet skor tertinggi (tanpa market maker):"]
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {short_wallet(row.wallet)} | skor {row.score:.0f} "
                f"| edge {_pct(row.edge)} | ROI {_pct(row.roi)} "
                f"| {row.markets_count} market | win {row.win_rate * 100:.0f}%"
            )
        return "\n".join(lines)


class WalletDetail:
    """/wallet <alamat>: statistik satu wallet + status watchlist."""

    def __init__(
        self,
        wallet_stats: WalletStatsRepository,
        users: UserRepository,
        watchlist: WatchlistRepository,
    ) -> None:
        self._wallet_stats = wallet_stats
        self._users = users
        self._watchlist = watchlist

    async def execute(self, telegram_id: int, address: str) -> str:
        wallet = normalize_wallet(address)  # raise InvalidWalletError
        stats = await self._wallet_stats.get(wallet)
        watched = await self._watched(telegram_id, wallet)
        if stats is None:
            return (
                f"{short_wallet(wallet)} belum punya statistik "
                "(belum ada trade pada market resolve yang tercatat)."
            )
        return self.format(stats, watched)

    async def _watched(self, telegram_id: int, wallet: str) -> bool:
        user_id = await self._users.get_id_by_telegram(telegram_id)
        if user_id is None:
            return False
        return await self._watchlist.is_watched(user_id, wallet)

    @staticmethod
    def format(stats: WalletStats, watched: bool) -> str:
        """Formatter murni (dites unit)."""
        mm = "ya" if stats.is_market_maker else "bukan"
        watch = "di-watch" if watched else "tidak di-watch"
        lines = [
            f"Wallet {short_wallet(stats.wallet)} ({watch}):",
            f"Skor: {stats.score:.0f}",
            f"Edge: {_pct(stats.edge)}",
            f"ROI: {_pct(stats.roi)}",
            f"PnL: {stats.pnl} USDC",
            f"Market: {stats.markets_count}",
            f"Win rate: {stats.win_rate * 100:.0f}% ({stats.wins}/{stats.markets_count})",
            f"Market maker: {mm}",
        ]
        if stats.is_market_maker:
            lines.append("Catatan: market maker — tidak masuk /top dan tidak alert.")
        return "\n".join(lines)


def short_wallet(wallet: str) -> str:
    """Bentuk pendek alamat: `0xab…cd12` (cukup untuk dibaca manusia)."""
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:6]}…{wallet[-4:]}"


__all__ = [
    "DEFAULT_TOP",
    "MAX_TOP",
    "InvalidWalletError",
    "TopWallets",
    "WalletDetail",
    "short_wallet",
]
