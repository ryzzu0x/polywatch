"""Integration test record_result end-to-end ke PostgreSQL + Gamma mock (T-009).

Alur: market CLOSED di DB → poll resolusi Gamma (mock respx, termasuk retry)
→ simpan market_results + transisi status ke RESOLVED. Idempoten lintas
siklus. Memakai `TEST_DATABASE_URL`; auto-skip bila env tidak ada.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
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

from polywatch.application.use_cases.record_result import RecordResult
from polywatch.domain.market import MarketStatus
from polywatch.infrastructure.db.models import AssetModel, Base, MarketModel, MarketResultModel
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
SLUG = "btc-updown-5m-1794024000"


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


class _InstantSleep:
    """Sleep instan supaya retry loop tidak menunggu nyata."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, seconds: float) -> None:
        self.calls += 1


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append(text)


class FakePriceSource:
    """PriceSource palsu — tidak ada HTTP di integration test ini."""

    def __init__(self, name: str, pair: tuple[Decimal, Decimal] | None) -> None:
        self.name = name
        self._pair = pair

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        return self._pair


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _seed_closed_market(session: AsyncSession) -> int:
    """Buat satu market berstatus CLOSED (seperti hasil discovery)."""
    session.add(AssetModel(symbol="btc", name="BTC", enabled_default=True))
    await session.flush()
    asset_id = (
        await session.execute(select(AssetModel.id).where(AssetModel.symbol == "btc"))
    ).scalar_one()
    market = MarketModel(
        condition_id="0xabc",
        slug=SLUG,
        asset_id=int(asset_id),
        timeframe="5m",
        start_at=datetime(2026, 11, 7, 4, 0, tzinfo=UTC),
        end_at=datetime(2026, 11, 7, 4, 5, tzinfo=UTC),
        token_up="111",
        token_down="222",
        status=MarketStatus.CLOSED,
    )
    session.add(market)
    await session.commit()
    return int(market.id)


def _resolved_payload() -> dict[str, object]:
    """Payload market resolved nyata dari fixture T-005 (outcomePrices ["1","0"] = Up)."""
    payload = dict(json.loads(FIXTURE.read_text())["response_truncated"])
    payload["slug"] = SLUG
    payload["closed"] = True
    payload["umaResolutionStatus"] = "resolved"
    payload["outcomePrices"] = '["1", "0"]'
    payload["volume"] = "42.068629"
    return payload


@respx.mock
async def test_record_result_end_to_end_sukses(session: AsyncSession) -> None:
    market_id = await _seed_closed_market(session)
    respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(200, json=[_resolved_payload()]))
    repo = SqlAlchemyMarketRepository(session)
    sleep = _InstantSleep()
    use_case = RecordResult(
        source=GammaMarketSource(GAMMA),
        repository=repo,
        primary_price=FakePriceSource(
            "polymarket-chainlink", (Decimal("75918.01"), Decimal("75920.97"))
        ),
        fallback_price=FakePriceSource("binance-1m", None),
        sleep=sleep,
    )

    summaries = await use_case.execute()
    await session.commit()

    assert [s.status for s in summaries] == ["resolved"]
    assert await _count(session, MarketResultModel) == 1
    row = (
        await session.execute(
            select(MarketResultModel).where(MarketResultModel.market_id == market_id)
        )
    ).scalar_one()
    assert row.result.value == "up"  # dari outcomePrices ["1","0"]
    assert row.open_price == Decimal("75918.01")
    assert row.close_price == Decimal("75920.97")
    assert row.volume_usdc == Decimal("42.068629")
    assert row.price_source == "polymarket-chainlink"  # selalu terisi

    market_row = (
        await session.execute(select(MarketModel).where(MarketModel.id == market_id))
    ).scalar_one()
    assert market_row.status is MarketStatus.RESOLVED
    assert sleep.calls == 0  # resolved pada poll pertama: tidak perlu retry


@respx.mock
async def test_record_result_idempoten_dua_siklus(session: AsyncSession) -> None:
    await _seed_closed_market(session)
    route = respx.get(f"{GAMMA}/markets").mock(
        return_value=httpx.Response(200, json=[_resolved_payload()])
    )
    repo = SqlAlchemyMarketRepository(session)
    use_case = RecordResult(
        source=GammaMarketSource(GAMMA),
        repository=repo,
        primary_price=FakePriceSource("polymarket-chainlink", (Decimal("1"), Decimal("2"))),
        sleep=_InstantSleep(),
    )

    await use_case.execute()
    await session.commit()
    await use_case.execute()  # siklus kedua
    await session.commit()

    assert await _count(session, MarketResultModel) == 1  # tidak duplikat
    assert route.call_count == 1  # siklus kedua: sudah resolved, tidak poll lagi


@respx.mock
async def test_record_result_retry_sebelum_resolved(session: AsyncSession) -> None:
    """Delay resolusi: poll pertama belum resolved, kedua sukses."""
    await _seed_closed_market(session)
    pending = _resolved_payload()
    pending["closed"] = True
    pending["umaResolutionStatus"] = None  # belum resolved
    pending["volume"] = None

    route = respx.get(f"{GAMMA}/markets").mock(
        side_effect=[
            httpx.Response(200, json=[pending]),
            httpx.Response(200, json=[_resolved_payload()]),
        ]
    )
    repo = SqlAlchemyMarketRepository(session)
    sleep = _InstantSleep()
    use_case = RecordResult(
        source=GammaMarketSource(GAMMA),
        repository=repo,
        primary_price=FakePriceSource("polymarket-chainlink", (Decimal("1"), Decimal("2"))),
        sleep=sleep,
    )

    summaries = await use_case.execute()
    await session.commit()

    assert [s.status for s in summaries] == ["resolved"]
    assert route.call_count == 2
    assert sleep.calls == 1  # satu jeda retry
    assert await _count(session, MarketResultModel) == 1


@respx.mock
async def test_record_result_timeout_notifikasi_owner(session: AsyncSession) -> None:
    market_id = await _seed_closed_market(session)
    pending = _resolved_payload()
    pending["umaResolutionStatus"] = None  # tidak pernah resolved
    respx.get(f"{GAMMA}/markets").mock(return_value=httpx.Response(200, json=[pending]))
    repo = SqlAlchemyMarketRepository(session)
    notifier = FakeNotifier()
    use_case = RecordResult(
        source=GammaMarketSource(GAMMA),
        repository=repo,
        primary_price=FakePriceSource("polymarket-chainlink", None),
        notifier=notifier,
        owner_chat_id=555,
        sleep=_InstantSleep(),
        poll_interval_sec=30.0,
        timeout_sec=90.0,  # 3 interval
    )

    summaries = await use_case.execute()
    await session.commit()

    assert [s.status for s in summaries] == ["timeout"]
    assert len(notifier.sent) == 1
    assert SLUG in notifier.sent[0]
    assert await _count(session, MarketResultModel) == 0  # tidak ada hasil palsu
    # market tetap CLOSED — bisa dicoba lagi siklus berikutnya
    market_row = (
        await session.execute(select(MarketModel).where(MarketModel.id == market_id))
    ).scalar_one()
    assert market_row.status is MarketStatus.CLOSED
