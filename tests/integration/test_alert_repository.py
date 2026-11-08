"""Integration test AlertSettingsRepository & AlertsSentRepository (T-017).

Memakai `TEST_DATABASE_URL`; auto-skip bila env tidak ada.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.infrastructure.db.models import (
    AlertSentModel,
    AlertSettingsModel,
    AssetModel,
    Base,
    MarketModel,
    TradeModel,
)
from polywatch.infrastructure.db.repositories.alert_repository import (
    SqlAlchemyAlertSettingsRepository,
    SqlAlchemyAlertsSentRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

USER = 1
WALLET = "0x" + "ab" * 20
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


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


async def test_settings_get_kosong_lalu_save_dan_upsert(session: AsyncSession) -> None:
    repo = SqlAlchemyAlertSettingsRepository(session)
    assert await repo.get(USER) == {}

    await repo.save(
        USER,
        {
            "min_score": Decimal("70"),
            "min_usdc": Decimal("100"),
            "cooldown_sec": 300,
            "enabled": True,
        },
    )
    await session.commit()
    values = await repo.get(USER)
    assert values["min_score"] == Decimal("70")
    assert values["cooldown_sec"] == 300
    assert values["enabled"] is True

    # upsert: nilai terakhir menang, tetap satu baris
    await repo.save(
        USER,
        {
            "min_score": Decimal("80"),
            "min_usdc": Decimal("500"),
            "cooldown_sec": 60,
            "enabled": False,
        },
    )
    await session.commit()
    values = await repo.get(USER)
    assert values["min_score"] == Decimal("80")
    assert values["enabled"] is False
    rows = (await session.execute(select(AlertSettingsModel))).scalars().all()
    assert len(rows) == 1


async def test_alerts_sent_record_dan_last_sent_at(session: AsyncSession) -> None:
    session.add(AssetModel(symbol="btc", name="Bitcoin", enabled_default=True))
    await session.commit()
    asset_id = (await session.execute(select(AssetModel))).scalar_one().id
    session.add(
        MarketModel(
            id=7,
            condition_id="0xcid",
            slug="btc-updown-5m-1000",
            asset_id=asset_id,
            timeframe="5m",
            start_at=T0,
            end_at=T0 + timedelta(minutes=5),
            token_up="111",
            token_down="222",
            status="live",
        )
    )
    session.add(
        TradeModel(
            tx_hash="0xtx1",
            log_index=1,
            market_id=7,
            wallet=WALLET,
            outcome="up",
            action="buy",
            price=Decimal("0.6"),
            size=Decimal("100"),
            usdc=Decimal("60"),
            traded_at=T0,
        )
    )
    await session.commit()

    repo = SqlAlchemyAlertsSentRepository(session)
    assert await repo.last_sent_at(USER, WALLET) is None

    sent_at = T0 + timedelta(seconds=3)
    await repo.record(
        user_id=USER,
        wallet=WALLET,
        market_id=7,
        tx_hash="0xtx1",
        log_index=1,
        sent_at=sent_at,
        delay_ms=3000,
    )
    await session.commit()

    assert await repo.last_sent_at(USER, WALLET) == sent_at
    row = (await session.execute(select(AlertSentModel))).scalars().one()
    assert row.delay_ms == 3000
    assert row.trade_id is not None  # terhubung ke baris trades

    # alert lebih lama tidak menimpa last_sent_at (max)
    older = T0 + timedelta(seconds=1)
    await repo.record(
        user_id=USER,
        wallet=WALLET,
        market_id=7,
        tx_hash="0xtx1",
        log_index=1,
        sent_at=older,
        delay_ms=1000,
    )
    await session.commit()
    assert await repo.last_sent_at(USER, WALLET) == sent_at
