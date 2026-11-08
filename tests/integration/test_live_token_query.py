"""Integration test SqlAlchemyLiveTokenQuery — jendela waktu + watch (T-026/T-028).

T-028 (ADR-015) — ingest berbasis jendela waktu, bukan status LIVE basi:
- market dengan status LIVE tapi end_at sudah lewat → TIDAK diambil;
- market aktif walau statusnya masih UPCOMING (status telat) → tetap diambil;
- market yang mulai <=5 dtk lagi sudah diambil (margin early subscribe);
- market RESOLVED tidak pernah diambil walau waktunya masih berjalan.

T-026/ADR-012 (semantik watch default-enabled) tetap diuji:
- tabel `watch_config` kosong → semua market aktif dikembalikan;
- override `enabled=false` satu pasangan asetxtimeframe → market aktif
  pasangan itu dikecualikan, pasangan lain tetap masuk.

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
    session: AsyncSession,
    symbol: str,
    timeframe: Timeframe,
    status: MarketStatus,
    *,
    start_at: datetime = NOW,
    end_at: datetime | None = None,
) -> int:
    """Buat aset + market (token up/down unik) lalu kembalikan market id."""
    asset = AssetModel(symbol=symbol, name=symbol.upper(), enabled_default=True)
    session.add(asset)
    await session.flush()
    market = MarketModel(
        condition_id=f"0xcid-{symbol}-{timeframe.value}-{int(start_at.timestamp())}",
        slug=f"{symbol}-updown-{timeframe.value}-{int(start_at.timestamp())}",
        asset_id=asset.id,
        timeframe=timeframe,
        start_at=start_at,
        end_at=end_at or start_at + timedelta(minutes=5),
        token_up=f"tok-up-{symbol}-{timeframe.value}",
        token_down=f"tok-down-{symbol}-{timeframe.value}",
        status=status,
    )
    session.add(market)
    await session.flush()
    return int(market.id)


class FixedClock:
    """Jam tetap untuk query berbasis jendela waktu (T-028)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def test_tabel_kosong_semua_market_live(session: AsyncSession) -> None:
    """T-026/T-028: watch_config kosong → semua market dalam jendela aktif."""
    await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.LIVE)
    await _seed_market(session, "eth", Timeframe.FIFTEEN_MINUTES, MarketStatus.LIVE)
    # upcoming yang belum mulai (jendela masa depan, >5 dtk) tidak ikut
    await _seed_market(
        session,
        "sol",
        Timeframe.FIVE_MINUTES,
        MarketStatus.UPCOMING,
        start_at=NOW + timedelta(minutes=10),
    )
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    # market dalam jendela waktunya aktif; upcoming masa depan tidak ikut;
    # watch_config kosong tidak mematikan apa pun (dulu: join inner → 0 baris)
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

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
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

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens([Timeframe.FIVE_MINUTES]))
    assert [t.slug for t in tokens] == [f"btc-updown-5m-{int(NOW.timestamp())}"]


# ------------------------------------------------------- T-028: jendela waktu


async def test_market_live_basi_tidak_diambil(session: AsyncSession) -> None:
    """T-028: status LIVE tapi end_at sudah lewat → token TIDAK diambil.

    Ini akar bug produksi: 38 market LIVE semuanya sudah lewat end_at,
    ingest subscribe market selesai → WS tak pernah mengirim trade.
    """
    await _seed_market(
        session,
        "btc",
        Timeframe.FIVE_MINUTES,
        MarketStatus.LIVE,  # status basi
        start_at=NOW - timedelta(minutes=10),
        end_at=NOW - timedelta(minutes=5),
    )
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    assert tokens == []  # end_at < now → dikecualikan walau status LIVE


async def test_market_aktif_status_upcoming_tetap_diambil(session: AsyncSession) -> None:
    """T-028: jendela berjalan walau status masih UPCOMING (status telat)."""
    aktif_id = await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.UPCOMING)
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    assert [t.market_id for t in tokens] == [aktif_id]


async def test_market_resolved_tidak_diambil_walau_jendela_berjalan(
    session: AsyncSession,
) -> None:
    """T-028: status RESOLVED tidak di-ingest ulang walau waktunya aktif."""
    await _seed_market(session, "btc", Timeframe.FIVE_MINUTES, MarketStatus.RESOLVED)
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    assert tokens == []


async def test_margin_early_subscribe_5_detik(session: AsyncSession) -> None:
    """T-028: market mulai 3 dtk lagi sudah diambil; 10 dtk lagi belum."""
    mulai_3dtk = await _seed_market(
        session,
        "btc",
        Timeframe.FIVE_MINUTES,
        MarketStatus.UPCOMING,
        start_at=NOW + timedelta(seconds=3),
    )
    await _seed_market(
        session,
        "eth",
        Timeframe.FIVE_MINUTES,
        MarketStatus.UPCOMING,
        start_at=NOW + timedelta(seconds=10),
    )
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    assert [t.market_id for t in tokens] == [mulai_3dtk]


async def test_kombinasi_basi_dan_aktif_hanya_aktif(session: AsyncSession) -> None:
    """T-028 skenario produksi: LIVE basi + aktif → hanya aktif yang diambil."""
    basi_id = await _seed_market(
        session,
        "btc",
        Timeframe.FIVE_MINUTES,
        MarketStatus.LIVE,
        start_at=NOW - timedelta(minutes=10),
        end_at=NOW - timedelta(minutes=5),
    )
    aktif_id = await _seed_market(
        session,
        "eth",
        Timeframe.FIVE_MINUTES,
        MarketStatus.UPCOMING,  # status telat, jendela berjalan
    )
    await session.commit()

    query = SqlAlchemyLiveTokenQuery(session, clock=FixedClock(NOW))
    tokens = list(await query.live_tokens())

    assert [t.market_id for t in tokens] == [aktif_id]
    assert all(t.market_id != basi_id for t in tokens)
