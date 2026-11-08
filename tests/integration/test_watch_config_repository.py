"""Integration test WatchConfigRepository + UserRepository + manage_settings (T-008).

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

from polywatch.application.use_cases.manage_settings import (
    ManageSettings,
    UnknownAssetError,
)
from polywatch.domain.market import Timeframe
from polywatch.infrastructure.db.models import (
    AssetModel,
    Base,
    UserModel,
    WatchConfigModel,
)
from polywatch.infrastructure.db.repositories.user_repository import (
    SqlAlchemyUserRepository,
)
from polywatch.infrastructure.db.repositories.watch_config_repository import (
    SqlAlchemyWatchConfigRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)


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


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _seed_asset(session: AsyncSession, symbol: str) -> int:
    session.add(AssetModel(symbol=symbol, name=symbol.upper(), enabled_default=True))
    await session.commit()
    asset = (
        await session.execute(select(AssetModel).where(AssetModel.symbol == symbol))
    ).scalar_one()
    return int(asset.id)


async def test_set_enabled_membuat_user_dan_config(session: AsyncSession) -> None:
    await _seed_asset(session, "btc")
    watch = SqlAlchemyWatchConfigRepository(session)

    await watch.set_enabled(1, "btc", Timeframe.FIVE_MINUTES, True)
    await session.commit()

    assert await _count(session, WatchConfigModel) == 1
    assert await _count(session, UserModel) == 0  # user dibuat lewat UserRepository


async def test_user_repository_idempoten(session: AsyncSession) -> None:
    users = SqlAlchemyUserRepository(session)

    id1 = await users.get_or_create_by_telegram(101)
    id2 = await users.get_or_create_by_telegram(101)
    await session.commit()

    assert id1 == id2
    assert await _count(session, UserModel) == 1
    assert await users.get_id_by_telegram(101) == id1
    assert await users.get_id_by_telegram(999) is None


async def test_set_enabled_upsert_tidak_duplikat(session: AsyncSession) -> None:
    await _seed_asset(session, "btc")
    users = SqlAlchemyUserRepository(session)
    watch = SqlAlchemyWatchConfigRepository(session)
    user_id = await users.get_or_create_by_telegram(101)

    await watch.set_enabled(user_id, "btc", Timeframe.FIVE_MINUTES, True)
    await watch.set_enabled(user_id, "btc", Timeframe.FIVE_MINUTES, False)
    await watch.set_enabled(user_id, "btc", Timeframe.FIVE_MINUTES, True)
    await session.commit()

    assert await _count(session, WatchConfigModel) == 1  # unik (user, asset, tf)
    assert await watch.is_enabled(user_id, "btc", Timeframe.FIVE_MINUTES)


async def test_set_enabled_kombinasi_tf_terpisah(session: AsyncSession) -> None:
    await _seed_asset(session, "btc")
    users = SqlAlchemyUserRepository(session)
    watch = SqlAlchemyWatchConfigRepository(session)
    user_id = await users.get_or_create_by_telegram(101)

    await watch.set_enabled(user_id, "btc", Timeframe.FIVE_MINUTES, True)
    await watch.set_enabled(user_id, "btc", Timeframe.FIFTEEN_MINUTES, False)
    await session.commit()

    entries = list(await watch.list_all(user_id))
    assert len(entries) == 2
    by_tf = {e.timeframe: e.enabled for e in entries}
    assert by_tf == {Timeframe.FIVE_MINUTES: True, Timeframe.FIFTEEN_MINUTES: False}


async def test_manage_settings_alur_lengkap(session: AsyncSession) -> None:
    """Toggle asetxtf via use case; filter enabled mengembalikan yang aktif saja."""
    await _seed_asset(session, "btc")
    await _seed_asset(session, "eth")
    use_case = ManageSettings(
        users=SqlAlchemyUserRepository(session),
        watch_config=SqlAlchemyWatchConfigRepository(session),
    )

    await use_case.set_watch_enabled(101, "BTC", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, True)
    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, True)
    await session.commit()

    # nonaktifkan btcx15m → siklus berikutnya (filter) tidak memprosesnya
    await use_case.set_watch_enabled(101, "BTC", Timeframe.FIFTEEN_MINUTES, False)
    await session.commit()

    enabled = await use_case.enabled_assets(101)
    assert [(e.asset, e.timeframe) for e in enabled] == [
        ("btc", Timeframe.FIVE_MINUTES),
        ("eth", Timeframe.FIFTEEN_MINUTES),
    ]

    all_entries = await use_case.list_watch(101)
    assert len(all_entries) == 3  # termasuk yang nonaktif

    # user baris tetap satu meski banyak toggle
    assert await _count(session, UserModel) == 1
    assert await _count(session, WatchConfigModel) == 3


async def test_manage_settings_aset_baru_ditolak(session: AsyncSession) -> None:
    use_case = ManageSettings(
        users=SqlAlchemyUserRepository(session),
        watch_config=SqlAlchemyWatchConfigRepository(session),
    )

    with pytest.raises(UnknownAssetError):
        await use_case.set_watch_enabled(101, "pepe", Timeframe.FIVE_MINUTES, True)
    assert await _count(session, WatchConfigModel) == 0


async def test_list_watch_user_baru_kosong(session: AsyncSession) -> None:
    use_case = ManageSettings(
        users=SqlAlchemyUserRepository(session),
        watch_config=SqlAlchemyWatchConfigRepository(session),
    )

    assert await use_case.list_watch(101) == []
    assert await use_case.enabled_assets(101) == []
