"""Integration test TradeRepository.insert_many idempoten (T-011).

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env
tidak ada. Skema via `Base.metadata`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.domain.market import MarketStatus, Outcome, Timeframe
from polywatch.domain.trade import Action, Trade
from polywatch.infrastructure.db.models import (
    AssetModel,
    Base,
    MarketModel,
    TradeModel,
)
from polywatch.infrastructure.db.repositories.trade_repository import (
    SqlAlchemyTradeRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

TRADED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


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


async def _market_id(session: AsyncSession) -> int:
    session.add(AssetModel(symbol="btc", name="Bitcoin"))
    await session.flush()
    asset_id = (await session.scalar(select(AssetModel.id).where(AssetModel.symbol == "btc"))) or 1
    market = MarketModel(
        condition_id="0xcid",
        slug="btc-updown-5m-1794122400",
        asset_id=asset_id,
        timeframe=Timeframe.FIVE_MINUTES,
        start_at=datetime(2026, 11, 8, 11, 55, tzinfo=UTC),
        end_at=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        token_up="111",
        token_down="222",
        status=MarketStatus.LIVE,
    )
    session.add(market)
    await session.flush()
    return int(market.id)


def _trade(market_id: int, wallet: str, log_index: int = 1) -> Trade:
    return Trade(
        tx_hash="0xtx1",
        log_index=log_index,
        market_id=market_id,
        wallet=wallet,
        outcome=Outcome.UP,
        action=Action.BUY,
        price=Decimal("0.5"),
        size=Decimal("10"),
        usdc=Decimal("5"),
        traded_at=TRADED_AT,
    )


async def test_insert_many_baru_dihitung(session: AsyncSession) -> None:
    market_id = await _market_id(session)
    repo = SqlAlchemyTradeRepository(session)
    saved = await repo.insert_many([_trade(market_id, "0x" + "ab" * 20)])
    assert saved == 1


async def test_insert_many_idempoten_duplikat(session: AsyncSession) -> None:
    market_id = await _market_id(session)
    repo = SqlAlchemyTradeRepository(session)
    wallet = "0x" + "ab" * 20
    first = await repo.insert_many([_trade(market_id, wallet)])
    second = await repo.insert_many([_trade(market_id, wallet)])
    assert first == 1
    assert second == 0  # ON CONFLICT (tx_hash, log_index, wallet) DO NOTHING
    rows = (await session.scalars(select(TradeModel))).all()
    assert len(rows) == 1


async def test_insert_many_dedupe_dalam_satu_batch(session: AsyncSession) -> None:
    market_id = await _market_id(session)
    repo = SqlAlchemyTradeRepository(session)
    wallet = "0x" + "ab" * 20
    saved = await repo.insert_many([_trade(market_id, wallet), _trade(market_id, wallet)])
    assert saved == 1
    rows = (await session.scalars(select(TradeModel))).all()
    assert len(rows) == 1


async def test_insert_many_log_index_berbeda_adalah_trade_berbeda(session: AsyncSession) -> None:
    market_id = await _market_id(session)
    repo = SqlAlchemyTradeRepository(session)
    wallet = "0x" + "ab" * 20
    saved = await repo.insert_many(
        [_trade(market_id, wallet, log_index=1), _trade(market_id, wallet, log_index=2)]
    )
    assert saved == 2


async def test_insert_many_batch_kosong(session: AsyncSession) -> None:
    repo = SqlAlchemyTradeRepository(session)
    assert await repo.insert_many([]) == 0


async def test_last_traded_at(session: AsyncSession) -> None:
    market_id = await _market_id(session)
    repo = SqlAlchemyTradeRepository(session)
    assert await repo.last_traded_at(market_id) is None
    await repo.insert_many([_trade(market_id, "0x" + "ab" * 20)])
    assert await repo.last_traded_at(market_id) == TRADED_AT
