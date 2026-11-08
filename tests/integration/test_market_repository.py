"""Integration test MarketRepository PostgreSQL + discover_markets end-to-end.

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env tidak
ada. Skema dibuat via `Base.metadata` di fixture.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from polywatch.domain.market import MarketStatus, Timeframe
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


async def _no_sleep(_: float) -> None:
    """No-op sleep — test integration jalan tanpa jeda antar-request slug."""
    return None


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
    """discover_markets dua kali dengan sumber Gamma (mock): tanpa duplikat.

    T-024: pencarian per slug — hanya jendela berjalan yang mengembalikan
    market, sisanya 404/empty. Verifikasi live: slug berisi window start yang
    dihitung dari waktu sekarang, bukan dari urutan API.
    """
    now = datetime.now(tz=UTC)
    current = int(now.timestamp()) // 300 * 300
    page = _gamma_page(1)
    page[0]["slug"] = f"btc-updown-5m-{current}"

    def route(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "slug" in params:
            # jalur utama per-slug: hanya market berjalan yang ada
            if params["slug"] == f"btc-updown-5m-{current}":
                return httpx.Response(200, json=page)
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    respx.get(f"{GAMMA}/markets").mock(side_effect=route)

    source = GammaMarketSource(GAMMA)
    repo = SqlAlchemyMarketRepository(session)
    use_case = DiscoverMarkets(
        source=source,
        repository=repo,
        clock=SystemClock(),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    )

    first = await use_case.execute()
    await session.commit()
    second = await use_case.execute()
    await session.commit()

    assert first.new_markets == 1
    assert first.total_markets == 1
    assert second.new_markets == 0
    assert second.total_markets == 1
    assert await _count(session, MarketModel) == 1  # tidak ada duplikat
    assert await _count(session, AssetModel) == 1  # semua btc
    stored = await repo.get_by_slug(f"btc-updown-5m-{current}")
    assert stored is not None
    assert stored.status in {MarketStatus.LIVE, MarketStatus.CLOSED}


@respx.mock
async def test_discover_market_basi_tidak_masuk(session: AsyncSession) -> None:
    """Market basi Des 2025 dari Gamma → ditolak filter anti-stale (T-024)."""
    stale = dict(_gamma_page(1)[0])
    stale["slug"] = "eth-updown-5m-1766161800"  # end 2025-12-19, ~11 bulan lalu
    stale["conditionId"] = "0xstale"

    def route(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "slug" in params:
            if params["slug"] == "eth-updown-5m-1766161800":
                return httpx.Response(200, json=[stale])
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[stale])  # jalur supplement

    respx.get(f"{GAMMA}/markets").mock(side_effect=route)

    use_case = DiscoverMarkets(
        source=GammaMarketSource(GAMMA),
        repository=SqlAlchemyMarketRepository(session),
        clock=SystemClock(),
        default_assets=("eth",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
        supplement_source=GammaMarketSource(GAMMA),
    )
    summary = await use_case.execute()
    await session.commit()

    assert summary.total_markets == 0
    assert summary.stale_filtered >= 1
    assert await _count(session, MarketModel) == 0


@respx.mock
async def test_discover_jendela_depan_dan_belakang_diquery(session: AsyncSession) -> None:
    """offset<0 memakai closed=true; jendela depan closed=false (T-024)."""
    current = int(datetime.now(tz=UTC).timestamp()) // 300 * 300
    seen: dict[str, str] = {}

    def route(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "slug" in params:
            seen[params["slug"]] = params["closed"]
        return httpx.Response(200, json=[])

    respx.get(f"{GAMMA}/markets").mock(side_effect=route)

    use_case = DiscoverMarkets(
        source=GammaMarketSource(GAMMA),
        repository=SqlAlchemyMarketRepository(session),
        clock=SystemClock(),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    )
    await use_case.execute()

    assert seen[f"btc-updown-5m-{current}"] == "false"
    assert seen[f"btc-updown-5m-{current + 300}"] == "false"
    assert seen[f"btc-updown-5m-{current + 600}"] == "false"
    assert seen[f"btc-updown-5m-{current - 300}"] == "true"
    assert seen[f"btc-updown-5m-{current - 600}"] == "true"
    assert seen[f"btc-updown-5m-{current - 900}"] == "true"


# ------------------------------------------------------- T-028: sweep status


async def test_list_stale_open_dua_arah(session: AsyncSession) -> None:
    """T-028: kandidat sweep = basi (end_at < now) + UPCOMING jendela berjalan."""

    from polywatch.domain.market import Market, MarketSlug

    now = datetime.now(tz=UTC)
    repo = SqlAlchemyMarketRepository(session)

    async def _mk(asset: str, slug: str, status: MarketStatus) -> int:
        return await repo.upsert(
            Market(
                condition_id=f"0x{slug}",
                slug=MarketSlug.parse(slug),
                token_up="111",
                token_down="222",
                status=status,
            )
        )

    # jendela 5m dibulatkan ke bawah supaya slug deterministik
    def _slug_window(asset: str, windows_ago: int) -> str:
        ts = int(now.timestamp())
        ts -= ts % 300
        return f"{asset}-updown-5m-{ts - windows_ago * 300}"

    live_basi = _slug_window("btc", 3)  # end ~15 mnt lalu
    upcoming_basi = _slug_window("btc", 7)  # end ~35 mnt lalu
    upcoming_berjalan = _slug_window("eth", 0)  # jendela berjalan, status telat
    live_aktif = _slug_window("sol", 0)  # jendela berjalan, status benar
    await _mk("btc", live_basi, MarketStatus.LIVE)
    await _mk("btc", upcoming_basi, MarketStatus.UPCOMING)
    await _mk("eth", upcoming_berjalan, MarketStatus.UPCOMING)
    await _mk("sol", live_aktif, MarketStatus.LIVE)
    await session.commit()

    stale = await repo.list_stale_open(now)
    slugs = {s.slug: s.status for s in stale}

    # basi dua-duanya + UPCOMING yang jendelanya berjalan; LIVE aktif tidak.
    assert set(slugs) == {live_basi, upcoming_basi, upcoming_berjalan}
    assert slugs[live_basi] is MarketStatus.LIVE
    assert slugs[upcoming_basi] is MarketStatus.UPCOMING
    assert slugs[upcoming_berjalan] is MarketStatus.UPCOMING
