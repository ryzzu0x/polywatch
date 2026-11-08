"""Skrip verifikasi manual T-024 (throwaway): discovery live ke Gamma sungguhan.

Jalankan: set -a; . ./.env; set +a; uv run python scripts/spike/t024_discover_live.py
Memakai TEST_DATABASE_URL (DB polywatch_test); tidak di-commit hasil DB-nya.

Bukti yang diminta: market live (target > 0, sebanding 16) benar-benar
tersimpan di DB — dihitung dari baris markets berstatus LIVE + jendela
berjalan/ke depan, plus market basi ditolak filter anti-stale.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import func, select
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

    gamma = GammaMarketSource("https://gamma-api.polymarket.com")

    async with factory() as session:
        repo = SqlAlchemyMarketRepository(session)
        discover = DiscoverMarkets(
            source=gamma,
            repository=repo,
            clock=SystemClock(),
            known_assets=repo,
            default_assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec"),
            fetch_delay_s=0.2,
        )
        summary = await discover.execute()
        await session.commit()

        total = int(await session.scalar(select(func.count()).select_from(MarketModel)) or 0)
        live = int(
            await session.scalar(
                select(func.count()).select_from(MarketModel).where(MarketModel.status == "LIVE")
            )
            or 0
        )
        upcoming = int(
            await session.scalar(
                select(func.count())
                .select_from(MarketModel)
                .where(MarketModel.status == "UPCOMING")
            )
            or 0
        )
        closed = int(
            await session.scalar(
                select(func.count()).select_from(MarketModel).where(MarketModel.status == "CLOSED")
            )
            or 0
        )
        assets = (await session.scalars(select(AssetModel.symbol))).all()
        print("=== T-024 discovery live ===")
        print(f"summary: {summary}")
        print(f"markets tersimpan: {total} (live={live} upcoming={upcoming} closed={closed})")
        print(f"assets: {sorted(assets)}")
        rows = (
            await session.execute(
                select(MarketModel.slug, MarketModel.status).order_by(MarketModel.slug)
            )
        ).all()
        for slug, status in rows:
            print(f"  {slug}  {status}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
