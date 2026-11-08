"""Integration test WatchlistRepository PostgreSQL (T-016).

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env tidak
ada. Skema dibuat via `Base.metadata` di fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.infrastructure.db.models import Base, WatchlistModel
from polywatch.infrastructure.db.repositories.watchlist_repository import (
    SqlAlchemyWatchlistRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

USER = 1
WALLET_A = "0x" + "ab" * 20
WALLET_B = "0x" + "cd" * 20


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DSN or "")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(WatchlistModel)) or 0)


async def test_add_list_is_watched(session: AsyncSession) -> None:
    repo = SqlAlchemyWatchlistRepository(session)
    await repo.add(USER, WALLET_A)
    await repo.add(USER, WALLET_B)
    await session.commit()

    assert await repo.is_watched(USER, WALLET_A) is True
    assert await repo.is_watched(USER, WALLET_B) is True
    assert await repo.is_watched(USER, "0x" + "ff" * 20) is False
    assert list(await repo.list(USER)) == [WALLET_A, WALLET_B]
    assert await _count(session) == 2


async def test_add_idempoten(session: AsyncSession) -> None:
    repo = SqlAlchemyWatchlistRepository(session)
    await repo.add(USER, WALLET_A)
    await session.commit()
    await repo.add(USER, WALLET_A)  # tidak error, tidak duplikat
    await session.commit()

    assert await _count(session) == 1


async def test_remove_dan_remove_hilang_aman(session: AsyncSession) -> None:
    repo = SqlAlchemyWatchlistRepository(session)
    await repo.add(USER, WALLET_A)
    await session.commit()

    await repo.remove(USER, WALLET_A)
    await session.commit()
    assert await repo.is_watched(USER, WALLET_A) is False

    await repo.remove(USER, WALLET_A)  # tidak error
    await session.commit()
    assert await _count(session) == 0


async def test_watchlist_terpisah_per_user(session: AsyncSession) -> None:
    repo = SqlAlchemyWatchlistRepository(session)
    await repo.add(USER, WALLET_A)
    await session.commit()

    assert await repo.is_watched(999, WALLET_A) is False
    assert list(await repo.list(999)) == []
