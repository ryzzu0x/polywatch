"""Integration test SqlAlchemyLiveTokenQuery — semantik watch default-enabled (T-026).

Membuktikan ADR-012 pada jalur query yang dipakai ingest:
- tabel `watch_config` kosong → SEMUA market LIVE dikembalikan;
- override `enabled=false` satu pasangan asetxtimeframe → market live
  pasangan itu dikecualikan, pasangan lain tetap masuk;
- override `enabled=true` eksplisit tidak mengubah hasil default.

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env
tidak ada. Skema dibuat via `Base.metadata` di fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.domain.market import MarketStatus, Timeframe
from polywatch.infrastructure.db.models import AssetModel, Base, MarketModel
from polywatch.infrastructure.db.repositories.live_token_query import (
    SqlAlchemyLiveTokenQuery,
)
from polywatch.infrastructure.db.repositories.watch_config_repository import (
    SqlAlchemyWatchConfigRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


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


async def _seed_market(
    session: AsyncSession, symbol: str, timeframe: Timeframe, status: MarketStatus
) -> int:
    """Buat aset + market (token up/down unik) lalu kembalikan market id."""
    asset = AssetModel(symbol=symbol, name=symbol.upper(), enabled_default=True)
    session.add(asset)
    await session.flush()
    market = MarketModel(
        condition_id=f"0xcid-{symbol}-{timeframe.value}",
        slug=f"{symbol}-updown-{timeframe.value}-1000",
        asset_id=asset.id,
        timeframe=timeframe,
        start_at=NOW,
        end_at=NOW + timedelta(minutes=5),
        token_up=f"tok-up-{symbol}-{timeframe.value}",
        token_down=f"tok-down-{symbol}-{timeframe.value}",
        status=status,
    )
    session.add(market)
    await session.flush()
    return int(market.id)


async def test_tabel_kosong_semua_market_live(session: AsyncSession) -> None:
    """T-026: watch_config kosong → semua market LIVE aktif (bug lama: 0)."""
    await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.LIVE)
    await _seed_market(session, "eth", Timeframe.FIFTEEN_MINUTES, MarketStatus.LIVE)
    await _seed_market(session, "sol", Timeframe.FIVE_MINUTES, MarketStatus.UPCOMING)
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session)
    tokens = list(await query.live_tokens())

    # hanya market LIVE; upcoming tidak ikut; watch_config kosong tidak
    # mematikan apa pun (dulu: join inner → 0 baris)
    assert {(t.slug.split("-updown")[0], t.token_up) for t in tokens} == {
        ("btc", "tok-up-btc-5m"),
        ("eth", "tok-up-eth-15m"),
    }


async def test_override_mematikan_satu_pasangan(session: AsyncSession) -> None:
    """T-026: override enabled=false mengecualikan hanya pasangan itu."""
    btc_id = await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.LIVE)
    eth_id = await _seed_market(session, "eth", Timeframe.FIVE_MINUTES, MarketStatus.LIVE)
    await session.commit()
    watch = SqlAlchemyWatchConfigRepository(session)

    # override mematikan btc 5m → hanya eth yang tersisa
    await watch.set_enabled(1, "btc", Timeframe.FIVE_MINUTES, False)
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session)
    tokens = list(await query.live_tokens())
    assert [t.market_id for t in tokens] == [eth_id]  # btc 5m dikecualikan

    # override enabled=true → btc kembali masuk (upsert, bukan baris baru)
    await watch.set_enabled(1, "btc", Timeframe.FIVE_MINUTES, True)
    await session.commit()
    tokens = list(await query.live_tokens())
    assert {t.market_id for t in tokens} == {btc_id, eth_id}


async def test_filter_timeframe(session: AsyncSession) -> None:
    """Argumen timeframes tetap membatasi hasil."""
    await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.LIVE)
    await _seed_market(session, "eth", Timeframe.FIFTEEN_MINUTES, MarketStatus.LIVE)
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session)
    tokens = list(await query.live_tokens([Timeframe.FIVE_MINUTES]))
    assert [t.slug for t in tokens] == ["btc-updown-5m-1000"]
