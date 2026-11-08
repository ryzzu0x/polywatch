"""Implementasi port UserRepository untuk PostgreSQL (MVP single-owner).

`get_or_create_by_telegram` idempoten lewat ON CONFLICT (telegram_id).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.infrastructure.db.models import UserModel


class SqlAlchemyUserRepository:
    """Repository users dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_id_by_telegram(self, telegram_id: int) -> int | None:
        stmt = select(UserModel.id).where(UserModel.telegram_id == telegram_id).limit(1)
        row = await self._session.scalar(stmt)
        return int(row) if row is not None else None

    async def get_or_create_by_telegram(self, telegram_id: int) -> int:
        stmt = (
            pg_insert(UserModel)
            .values(telegram_id=telegram_id)
            .on_conflict_do_update(
                index_elements=["telegram_id"],
                set_={"telegram_id": telegram_id},
            )
            .returning(UserModel.id)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
