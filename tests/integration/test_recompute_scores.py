"""Integration test recompute_scores + WalletStatsRepository (T-013).

Memakai `TEST_DATABASE_URL` (DB `polywatch_test`); auto-skip bila env
tidak ada. Menyiapkan market resolved + trades, menjalankan use case,
lalu memverifikasi baris `wallet_stats` (win_rate, edge, roi, score,
is_market_maker) dan idempotensi (jalankan 2x hasil sama).
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

from polywatch.application.use_cases.recompute_scores import RecomputeScores
from polywatch.domain.market import MarketStatus, Outcome, Timeframe
from polywatch.domain.scoring import ScoringPolicy
from polywatch.domain.trade import Action
from polywatch.infrastructure.db.models import (
    AssetModel,
    Base,
    MarketModel,
    MarketResultModel,
    TradeModel,
    WalletStatsModel,
)
from polywatch.infrastructure.db.repositories.trade_repository import (
    SqlAlchemyTradeRepository,
)
from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
    SqlAlchemyWalletStatsRepository,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

_T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_WALLET_A = "0x" + "aa" * 20
_WALLET_MM = "0x" + "bb" * 20


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


async def _seed_market(session: AsyncSession, market_id: int, result: Outcome) -> None:
    """Market RESOLVED + hasil resmi (asset_id 1, btc)."""
    session.add(
        MarketModel(
            id=market_id,
            condition_id=f"0xcond{market_id:04d}",
            slug=f"btc-updown-5m-{market_id}",
            asset_id=1,
            timeframe=Timeframe.FIVE_MINUTES,
            start_at=_T0 + timedelta(minutes=market_id),
            end_at=_T0 + timedelta(minutes=market_id + 5),
            token_up="tok-up",
            token_down="tok-down",
            status=MarketStatus.RESOLVED,
        )
    )
    session.add(
        MarketResultModel(
            market_id=market_id,
            open_price=Decimal("1000"),
            close_price=Decimal("1005") if result is Outcome.UP else Decimal("995"),
            result=result,
            volume_usdc=Decimal("10000"),
            price_source="polymarket",
            resolved_at=_T0 + timedelta(minutes=market_id + 6),
        )
    )
    await session.flush()


async def _seed_trade(
    session: AsyncSession,
    market_id: int,
    wallet: str,
    outcome: Outcome,
    price: str,
    usdc: str,
    tx: int,
) -> None:
    price_d = Decimal(price)
    usdc_d = Decimal(usdc)
    session.add(
        TradeModel(
            tx_hash=f"0x{tx:064x}",
            log_index=tx,
            market_id=market_id,
            wallet=wallet,
            outcome=outcome,
            action=Action.BUY,
            price=price_d,
            size=usdc_d / price_d,
            usdc=usdc_d,
            traded_at=_T0 + timedelta(minutes=tx),
        )
    )
    await session.flush()


async def _seed_closed_market_without_result(session: AsyncSession, market_id: int) -> None:
    """Market CLOSED tanpa hasil — trade di sini tidak boleh dihitung."""
    session.add(
        MarketModel(
            id=market_id,
            condition_id=f"0xcond{market_id:04d}",
            slug=f"btc-updown-5m-{market_id}",
            asset_id=1,
            timeframe=Timeframe.FIVE_MINUTES,
            start_at=_T0 + timedelta(minutes=market_id),
            end_at=_T0 + timedelta(minutes=market_id + 5),
            token_up="tok-up",
            token_down="tok-down",
            status=MarketStatus.CLOSED,
        )
    )
    await session.flush()


async def _make_use_case(session: AsyncSession, min_sample: int = 2) -> RecomputeScores:
    return RecomputeScores(
        trades=SqlAlchemyTradeRepository(session),
        wallet_stats=SqlAlchemyWalletStatsRepository(session),
        policy=ScoringPolicy(min_sample=min_sample),
    )


async def _wallet_row(session: AsyncSession, wallet: str) -> WalletStatsModel | None:
    return await session.scalar(select(WalletStatsModel).where(WalletStatsModel.wallet == wallet))


async def test_recompute_mengisi_wallet_stats_dan_idempoten(session: AsyncSession) -> None:
    """3 market resolved + 1 CLOSED; wallet A menang 2 dari 3."""
    session.add(AssetModel(symbol="btc", name="BTC", enabled_default=True))
    await session.flush()

    await _seed_market(session, 1, Outcome.UP)
    await _seed_market(session, 2, Outcome.DOWN)
    await _seed_market(session, 3, Outcome.UP)
    await _seed_closed_market_without_result(session, 4)

    # wallet A: beli UP di 0.50 (USDC 50) di market 1-3; kalah hanya market 2
    await _seed_trade(session, 1, _WALLET_A, Outcome.UP, "0.50", "50", tx=1)
    await _seed_trade(session, 2, _WALLET_A, Outcome.UP, "0.50", "50", tx=2)
    await _seed_trade(session, 3, _WALLET_A, Outcome.UP, "0.50", "50", tx=3)
    # market 4 belum resolve: tidak ikut statistik
    await _seed_trade(session, 4, _WALLET_A, Outcome.UP, "0.50", "50", tx=4)
    await session.commit()

    use_case = await _make_use_case(session)
    summary = await use_case.execute(3)
    await session.commit()

    assert summary.wallets_processed == 1
    row = await _wallet_row(session, _WALLET_A)
    assert row is not None

    # 3 market resolved (bukan 4), menang 2 → win_rate ≈ 2/3 (NUMERIC(6,4))
    assert row.markets_count == 3
    assert row.wins == 2
    assert row.win_rate == Decimal("0.6667")
    assert row.avg_entry == Decimal("0.50")
    assert row.edge == Decimal("0.1667")
    assert row.pnl == Decimal("50")  # 2x(+50) + 1x(-50)
    assert row.roi == Decimal("0.3333")
    assert row.is_market_maker is False
    assert row.score > 0

    first = {
        "markets_count": row.markets_count,
        "wins": row.wins,
        "win_rate": row.win_rate,
        "edge": row.edge,
        "pnl": row.pnl,
        "roi": row.roi,
        "score": row.score,
        "is_market_maker": row.is_market_maker,
    }

    # idempoten: jalankan lagi dari sumber sama
    await use_case.execute(3)
    await session.commit()

    row2 = await _wallet_row(session, _WALLET_A)
    assert row2 is not None
    second = {
        "markets_count": row2.markets_count,
        "wins": row2.wins,
        "win_rate": row2.win_rate,
        "edge": row2.edge,
        "pnl": row2.pnl,
        "roi": row2.roi,
        "score": row2.score,
        "is_market_maker": row2.is_market_maker,
    }
    assert first == second

    count = len((await session.execute(select(WalletStatsModel))).scalars().all())
    assert count == 1  # tidak ada duplikat baris


async def test_recompute_wallet_stats_get_dan_top(session: AsyncSession) -> None:
    """Repository `get` dan `top` (top mengexclude market maker)."""
    session.add(AssetModel(symbol="btc", name="BTC", enabled_default=True))
    await session.flush()
    await _seed_market(session, 1, Outcome.UP)
    await _seed_market(session, 2, Outcome.UP)
    await _seed_market(session, 3, Outcome.UP)

    # wallet A: sisi UP semua → menang, bukan market maker
    for m in (1, 2, 3):
        await _seed_trade(session, m, _WALLET_A, Outcome.UP, "0.50", "50", tx=m)
    # wallet MM: dua sisi di semua market → market maker
    for m in (1, 2, 3):
        await _seed_trade(session, m, _WALLET_MM, Outcome.UP, "0.50", "50", tx=10 + m)
        await _seed_trade(session, m, _WALLET_MM, Outcome.DOWN, "0.50", "50", tx=20 + m)
    await session.commit()

    use_case = await _make_use_case(session, min_sample=3)
    await use_case.execute(3)
    await session.commit()

    stats_repo = SqlAlchemyWalletStatsRepository(session)
    mm = await stats_repo.get(_WALLET_MM)
    assert mm is not None
    assert mm.is_market_maker is True
    assert mm.score == Decimal(0)

    top = list(await stats_repo.top(10))
    assert [s.wallet for s in top] == [_WALLET_A]  # MM dikeluarkan dari top
    assert top[0].score > Decimal(0)
    assert await stats_repo.get("0x" + "cc" * 20) is None


async def test_recompute_market_tanpa_trade_tidak_menulis_baris(session: AsyncSession) -> None:
    session.add(AssetModel(symbol="btc", name="BTC", enabled_default=True))
    await session.flush()
    await _seed_market(session, 1, Outcome.UP)
    await session.commit()

    use_case = await _make_use_case(session)
    summary = await use_case.execute(1)
    await session.commit()

    assert summary.wallets_processed == 0
    assert await _wallet_row(session, _WALLET_A) is None
    rows = (await session.execute(select(WalletStatsModel))).scalars().all()
    assert len(rows) == 0
