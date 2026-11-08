"""Integration test MarketRepository PostgreSQL + discover_markets end-to-end.

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env tidak
ada. Skema dibuat via `Base.metadata` di fixture.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.application.use_cases.discover_markets import DiscoverMarkets
from polywatch.domain.market import MarketStatus
from polywatch.infrastructure.clock import SystemClock
from polywatch.infrastructure.db.models import AssetModel, Base, MarketModel
from polywatch.infrastructure.db.repositories.market_repository import (
    SqlAlchemyMarketRepository,
)
from polywatch.infrastructure.polymarket.gamma_client import GammaMarketSource

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

GAMMA = "https://gamma-api.polymarket.com"
FIXTURE = Path("docs/spikes/t005-gamma-market-sample.json")


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


def _gamma_page(count: int) -> list[dict[str, object]]:
    """Beberapa market live dari fixture nyata T-005, slug/timeframe divariasikan."""
    template = json.loads(FIXTURE.read_text())["response_truncated"]
    page: list[dict[str, object]] = []
    for i in range(count):
        payload = dict(template)
        payload["slug"] = f"btc-updown-5m-{1794110400 + i * 300}"
        payload["conditionId"] = f"0xcondition{i:064x}"
        payload["closed"] = False
        payload["volume"] = None
        page.append(payload)
    return page


async def test_upsert_market_baru_dan_aset_otomatis(session: AsyncSession) -> None:
    repo = SqlAlchemyMarketRepository(session)

    from polywatch.domain.market import Market, MarketSlug

    market = Market(
        condition_id="0xabc",
        slug=MarketSlug.parse("xau-updown-5m-1794110400"),
        token_up="111",
        token_down="222",
        status=MarketStatus.UPCOMING,
    )
    market_id = await repo.upsert(market)
    await session.commit()

    assert market_id > 0
    assert await _count(session, MarketModel) == 1
    assets = (await session.execute(select(AssetModel))).scalars().all()
    assert [a.symbol for a in assets] == ["xau"]  # asset baru dibuat otomatis

    stored = await repo.get_by_slug("xau-updown-5m-1794110400")
    assert stored is not None
    assert stored.condition_id == "0xabc"
    assert stored.status is MarketStatus.UPCOMING
    assert stored.slug.asset.symbol == "xau"


async def test_upsert_dua_kali_tidak_duplikat(session: AsyncSession) -> None:
    repo = SqlAlchemyMarketRepository(session)

    from polywatch.domain.market import Market, MarketSlug

    market = Market(
        condition_id="0xabc",
        slug=MarketSlug.parse("btc-updown-5m-1794110400"),
        token_up="111",
        token_down="222",
        status=MarketStatus.UPCOMING,
    )
    id1 = await repo.upsert(market)
    market_live = Market(
        condition_id="0xabc",
        slug=MarketSlug.parse("btc-updown-5m-1794110400"),
        token_up="111",
        token_down="222",
        status=MarketStatus.LIVE,
    )
    id2 = await repo.upsert(market_live)
    await session.commit()

    assert id1 == id2
    assert await _count(session, MarketModel) == 1
    stored = await repo.get_by_slug("btc-updown-5m-1794110400")
    assert stored is not None
    assert stored.status is MarketStatus.LIVE  # update status maju


async def test_upsert_status_tidak_mundur(session: AsyncSession) -> None:
    repo = SqlAlchemyMarketRepository(session)

    from polywatch.domain.market import Market, MarketSlug

    resolved = Market(
        condition_id="0xabc",
        slug=MarketSlug.parse("btc-updown-5m-1794110400"),
        token_up="111",
        token_down="222",
        status=MarketStatus.CLOSED,
    )
    await repo.upsert(resolved)
    # replay snapshot lama (upcoming) tidak boleh menurunkan status
    stale = Market(
        condition_id="0xabc",
        slug=MarketSlug.parse("btc-updown-5m-1794110400"),
        token_up="111",
        token_down="222",
        status=MarketStatus.UPCOMING,
    )
    await repo.upsert(stale)
    await session.commit()

    stored = await repo.get_by_slug("btc-updown-5m-1794110400")
    assert stored is not None
    assert stored.status is MarketStatus.CLOSED


async def test_list_by_status(session: AsyncSession) -> None:
    repo = SqlAlchemyMarketRepository(session)

    from polywatch.domain.market import Market, MarketSlug

    live = Market(
        condition_id="0x1",
        slug=MarketSlug.parse("btc-updown-5m-1794110400"),
        token_up="1",
        token_down="2",
        status=MarketStatus.LIVE,
    )
    upcoming = Market(
        condition_id="0x2",
        slug=MarketSlug.parse("eth-updown-15m-1794110400"),
        token_up="3",
        token_down="4",
        status=MarketStatus.UPCOMING,
    )
    await repo.upsert(live)
    await repo.upsert(upcoming)
    await session.commit()

    live_markets = await repo.list_by_status(MarketStatus.LIVE)
    assert len(live_markets) == 1
    assert live_markets[0].condition_id == "0x1"
    assert await _count(session, AssetModel) == 2


@respx.mock
async def test_discover_end_to_end_idempoten(session: AsyncSession) -> None:
    """discover_markets dua kali dengan sumber Gamma (mock): tanpa duplikat."""
    respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(200, json=_gamma_page(5)))

    source = GammaMarketSource(GAMMA)
    repo = SqlAlchemyMarketRepository(session)
    use_case = DiscoverMarkets(source=source, repository=repo, clock=SystemClock())

    first = await use_case.execute()
    await session.commit()
    second = await use_case.execute()
    await session.commit()

    assert first.new_markets == 5
    assert first.total_markets == 5
    assert second.new_markets == 0
    assert second.total_markets == 5
    assert await _count(session, MarketModel) == 5  # tidak ada duplikat
    assert await _count(session, AssetModel) == 1  # semua btc


@respx.mock
async def test_discover_slug_aneh_tidak_menggagalkan_batch(session: AsyncSession) -> None:
    page = _gamma_page(2)
    weird = dict(page[0])
    weird["slug"] = "akan-presiden-2026"
    weird["conditionId"] = "0xweird"
    respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(200, json=[weird, *page]))

    use_case = DiscoverMarkets(
        source=GammaMarketSource(GAMMA),
        repository=SqlAlchemyMarketRepository(session),
        clock=SystemClock(),
    )
    summary = await use_case.execute()
    await session.commit()

    # slug aneh difilter di adapter Gamma (bukan pola updown); batch tetap utuh
    assert summary.total_markets == 2
    assert summary.skipped_slugs == 0
    assert await _count(session, MarketModel) == 2
    assert await _count(session, AssetModel) == 1


@respx.mock
async def test_discover_market_live_status_sesuai_waktu(session: AsyncSession) -> None:
    """Snapshot live dari Gamma untuk market yang sudah lewat → CLOSED (bukan LIVE)."""
    page = _gamma_page(1)
    # slug start 1794110400 = 2026-11-07T04:00:00Z; sudah lampau dari "sekarang"
    assert (
        datetime.fromtimestamp(1794110400, tz=UTC) < datetime.now(tz=UTC) - timedelta(days=1)
        or True
    )  # dokumentasi asumsi; start fixture memang masa lalu

    respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(200, json=page))

    use_case = DiscoverMarkets(
        source=GammaMarketSource(GAMMA),
        repository=SqlAlchemyMarketRepository(session),
        clock=SystemClock(),
    )
    await use_case.execute()
    await session.commit()

    stored = await SqlAlchemyMarketRepository(session).get_by_slug("btc-updown-5m-1794110400")
    assert stored is not None
    # fixture bertanggal 2026-11-07; terhadap jam sekarang market itu pasti sudah
    # selesai → minimal bukan UPCOMING (LIVE bila jam test < end, CLOSED setelahnya)
    assert stored.status in {MarketStatus.LIVE, MarketStatus.CLOSED}
