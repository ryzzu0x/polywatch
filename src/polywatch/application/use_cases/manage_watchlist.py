"""Use case manage_watchlist: /watch dan /unwatch (T-016).

Idempoten: watch wallet yang sudah ada tidak mengubah apa pun; unwatch
wallet yang tidak ada juga aman. Baris `users` dibuat otomatis bila belum
ada (MVP single-owner).
"""

from __future__ import annotations

import logging

from polywatch.application.ports import UserRepository, WatchlistRepository
from polywatch.application.use_cases.wallet_views import short_wallet
from polywatch.domain.errors import InvalidWalletError
from polywatch.domain.trade import normalize_wallet

logger = logging.getLogger(__name__)


class ManageWatchlist:
    """/watch & /unwatch untuk satu pengguna (telegram_id)."""

    def __init__(self, users: UserRepository, watchlist: WatchlistRepository) -> None:
        self._users = users
        self._watchlist = watchlist

    async def watch(self, telegram_id: int, address: str) -> str:
        """Tambahkan wallet ke watchlist; balasan singkat."""
        wallet = normalize_wallet(address)  # raise InvalidWalletError
        user_id = await self._users.get_or_create_by_telegram(telegram_id)
        already = await self._watchlist.is_watched(user_id, wallet)
        if not already:
            await self._watchlist.add(user_id, wallet)
            logger.info(
                "wallet di-watch",
                extra={"event": "watchlist_add", "telegram_id": telegram_id, "wallet": wallet},
            )
        status = "sudah ada di watchlist" if already else "masuk watchlist"
        return f"{short_wallet(wallet)} {status}."

    async def unwatch(self, telegram_id: int, address: str) -> str:
        """Hapus wallet dari watchlist; balasan singkat."""
        wallet = normalize_wallet(address)  # raise InvalidWalletError
        user_id = await self._users.get_or_create_by_telegram(telegram_id)
        was = await self._watchlist.is_watched(user_id, wallet)
        if was:
            await self._watchlist.remove(user_id, wallet)
            logger.info(
                "wallet di-unwatch",
                extra={"event": "watchlist_remove", "telegram_id": telegram_id, "wallet": wallet},
            )
        status = "dikeluarkan dari watchlist" if was else "memang tidak ada di watchlist"
        return f"{short_wallet(wallet)} {status}."


__all__ = ["InvalidWalletError", "ManageWatchlist"]
