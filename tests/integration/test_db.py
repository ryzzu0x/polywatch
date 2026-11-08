"""Integration test DB: koneksi + migrasi + repo dasar (T-004).

Memakai `TEST_DATABASE_URL`. Test di-skip otomatis bila env tidak ada
(jangan gagal di CI). Skema dibuat via `Base.metadata` di fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.domain.market import MarketStatus, Outcome, Timeframe
from polywatch.domain.trade import Action
from polywatch.infrastructure.db.models import (
    AssetModel,
    MarketModel,
    TradeModel,
    UserModel,
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
        from polywatch.infrastructure.db.models import Base

        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_koneksi_dan_tabel_dibuat(session: AsyncSession) -> None:
    from polywatch.infrastructure.db.models import Base

    expected = {
        "assets",
        "watch_config",
        "markets",
        "market_results",
        "trades",
        "positions",
        "wallet_stats",
        "watchlist",
        "alert_settings",
        "alerts_sent",
        "users",
    }
    assert expected <= set(Base.metadata.tables)


_TRADE_ROW = {
    "tx_hash": "0xdead01",
    "log_index": 1,
    "market_id": 1,
    "wallet": "0x" + "ab" * 20,
    "outcome": Outcome.UP,
    "action": Action.BUY,
    "price": 0.55,
    "size": 100.0,
    "usdc": 55.0,
    "traded_at": datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
}


async def test_insert_dan_query_market_trade(session: AsyncSession) -> None:
    session.add(AssetModel(symbol="btc", name="Bitcoin", enabled_default=True))
    await session.flush()
    asset_id = (await session.scalars(select(AssetModel))).one().id

    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    session.add(
        MarketModel(
            condition_id="0xabc",
            slug="btc-updown-5m-1762603200",
            asset_id=asset_id,
            timeframe=Timeframe.FIVE_MINUTES,
            start_at=start,
            end_at=start + Timeframe.FIVE_MINUTES.duration,
            token_up="tok-up",
            token_down="tok-down",
            status=MarketStatus.LIVE,
        )
    )
    await session.flush()
    market = (await session.scalars(select(MarketModel))).one()
    assert market.slug == "btc-updown-5m-1762603200"

    row = dict(_TRADE_ROW, market_id=market.id, traded_at=start)
    await session.execute(insert(TradeModel), row)
    await session.commit()
    trades = list(await session.scalars(select(TradeModel)))
    assert len(trades) == 1, "idempotensi UNIQUE(tx_hash, log_index, wallet)"

    await session.rollback()

    async def insert_duplikat() -> None:
        await session.execute(insert(TradeModel), row)
        await session.commit()

    with pytest.raises(IntegrityError):
        await insert_duplikat()
    await session.rollback()


async def test_users_telegram_id_unik(session: AsyncSession) -> None:
    session.add(UserModel(telegram_id=513804114))
    session.add(UserModel(telegram_id=513804114))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_last_trade_at_live_hanya_market_live(session: AsyncSession) -> None:
    """T-019: bacaan aktivitas health monitor — trade terakhir market LIVE saja."""
    from polywatch.infrastructure.db.repositories.trade_repository import (
        SqlAlchemyTradeRepository,
    )

    session.add(AssetModel(symbol="eth", name="Ethereum", enabled_default=True))
    await session.flush()
    asset_id = (await session.scalars(select(AssetModel))).one().id

    start = datetime(2026, 11, 8, 13, 0, tzinfo=UTC)
    for status in (MarketStatus.LIVE, MarketStatus.RESOLVED):
        session.add(
            MarketModel(
                condition_id=f"0x{status.value}",
                slug=f"eth-updown-5m-1762606800-{status.value}",
                asset_id=asset_id,
                timeframe=Timeframe.FIVE_MINUTES,
                start_at=start,
                end_at=start + Timeframe.FIVE_MINUTES.duration,
                token_up="tok-up",
                token_down="tok-down",
                status=status,
            )
        )
    await session.flush()
    markets = {m.status.value: m.id for m in await session.scalars(select(MarketModel))}
    live_id, resolved_id = markets["live"], markets["resolved"]

    await session.execute(
        insert(TradeModel),
        dict(_TRADE_ROW, market_id=live_id, traded_at=start + timedelta(minutes=1)),
    )
    await session.execute(
        insert(TradeModel),
        dict(
            _TRADE_ROW,
            tx_hash="0xdead02",
            log_index=2,
            market_id=resolved_id,
            traded_at=start + timedelta(minutes=2),  # lebih baru tapi bukan LIVE
        ),
    )
    await session.commit()

    repo = SqlAlchemyTradeRepository(session)
    last = await repo.last_trade_at_live()
    assert last == start + timedelta(minutes=1)


async def test_last_trade_at_live_kosong(session: AsyncSession) -> None:
    """Tanpa trade sama sekali → None (grace period health monitor)."""
    from polywatch.infrastructure.db.repositories.trade_repository import (
        SqlAlchemyTradeRepository,
    )

    repo = SqlAlchemyTradeRepository(session)
    assert await repo.last_trade_at_live() is None
