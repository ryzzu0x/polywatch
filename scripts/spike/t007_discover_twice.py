"""Skrip verifikasi manual T-007 (throwaway): discover dua kali ke Gamma live.

Jalankan: uv run python scripts/spike/t007_discover_twice.py
Memakai TEST_DATABASE_URL; tidak di-commit hasil DB-nya.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polywatch.application.use_cases.discover_markets import DiscoverMarkets
from polywatch.infrastructure.clock import SystemClock
from polywatch.infrastructure.db.models import AssetModel, Base, MarketModel
from polywatch.infrastructure.db.repositories.market_repository import (
    SqlAlchemyMarketRepository,
)
from polywatch.infrastructure.polymarket.gamma_client import GammaMarketSource

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def main() -> None:
    dsn = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        use_case = DiscoverMarkets(
            source=GammaMarketSource("https://gamma-api.polymarket.com"),
            repository=SqlAlchemyMarketRepository(session),
            clock=SystemClock(),
        )
        first = await use_case.execute()
        await session.commit()
        second = await use_case.execute()
        await session.commit()

        markets = int(await session.scalar(select(func.count()).select_from(MarketModel)) or 0)
        assets = int(await session.scalar(select(func.count()).select_from(AssetModel)) or 0)
        distinct_conditions = int(
            await session.scalar(text("select count(distinct condition_id) from markets")) or 0
        )
        print(
            f"run#1: new={first.new_markets} total={first.total_markets} skip={first.skipped_slugs}"
        )
        print(
            f"run#2: new={second.new_markets} total={second.total_markets}"
            f" skip={second.skipped_slugs}"
        )
        print(f"markets={markets} distinct_condition_id={distinct_conditions} assets={assets}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
