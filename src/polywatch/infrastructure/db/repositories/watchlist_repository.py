"""Implementasi port WatchlistRepository untuk PostgreSQL (T-016).

Idempoten: `add` memakai ON CONFLICT DO NOTHING, `remove` menghapus baris
bila ada (tidak error bila tidak ada).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.infrastructure.db.models import WatchlistModel


class SqlAlchemyWatchlistRepository:
    """Repository watchlist dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user_id: int, wallet: str) -> None:
        """Tambah wallet ke watchlist pengguna (idempoten)."""
        stmt = (
            pg_insert(WatchlistModel)
            .values(user_id=user_id, wallet=wallet)
            .on_conflict_do_nothing()
        )
        await self._session.execute(stmt)

    async def remove(self, user_id: int, wallet: str) -> None:
        """Hapus wallet dari watchlist pengguna (aman bila tidak ada)."""
        stmt = delete(WatchlistModel).where(
            WatchlistModel.user_id == user_id,
            WatchlistModel.wallet == wallet,
        )
        await self._session.execute(stmt)

    async def list(self, user_id: int) -> Sequence[str]:
        """Daftar wallet yang di-watch pengguna (urut abjad)."""
        stmt = (
            select(WatchlistModel.wallet)
            .where(WatchlistModel.user_id == user_id)
            .order_by(WatchlistModel.wallet)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def is_watched(self, user_id: int, wallet: str) -> bool:
        stmt = select(WatchlistModel.wallet).where(
            WatchlistModel.user_id == user_id,
            WatchlistModel.wallet == wallet,
        )
        return await self._session.scalar(stmt) is not None
