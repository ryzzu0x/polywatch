"""Unit test use case discover_markets — tanpa I/O (fake source/repository)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polywatch.application.dto.market import MarketSnapshot, PendingMarket
from polywatch.application.use_cases.discover_markets import DiscoverMarkets
from polywatch.domain.market import Market, MarketSlug, MarketStatus, Timeframe


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeSource:
    """MarketSource palsu; mengembalikan snapshot yang dikonfigurasi per timeframe."""

    def __init__(self, by_timeframe: dict[Timeframe, Sequence[MarketSnapshot]]) -> None:
        self._by_timeframe = by_timeframe

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
        return self._by_timeframe.get(timeframe, [])

    async def get_result(self, slug: str) -> None:
        return None


class FakeRepository:
    """MarketRepository in-memory; upsert idempoten per condition_id."""

    def __init__(self) -> None:
        self.by_condition: dict[str, Market] = {}
        self.upsert_calls = 0

    async def upsert(self, market: Market) -> int:
        self.upsert_calls += 1
        if market.condition_id not in self.by_condition:
            self.by_condition[market.condition_id] = market
            return len(self.by_condition)
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        for market in self.by_condition.values():
            if market.slug == MarketSlug.parse(slug):
                return market
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return [m for m in self.by_condition.values() if m.status is status]

    async def get_result(self, market_id: int) -> None:
        return None

    async def save_result(self, result: object) -> None:
        return None

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        return None

    async def has_result(self, market_id: int) -> bool:
        return False

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return []


def _snapshot(slug: str, condition_id: str, volume: str = "10") -> MarketSnapshot:
    return MarketSnapshot(
        slug=slug,
        condition_id=condition_id,
        token_up="111",
        token_down="222",
        volume_usdc=Decimal(volume),
        closed=False,
    )


async def test_discover_baru_menghitung_dan_menyimpan() -> None:
    now = datetime.fromtimestamp(1762560000, tz=UTC) + timedelta(minutes=2)
    source = FakeSource(
        {
            Timeframe.FIVE_MINUTES: [
                _snapshot("btc-updown-5m-1762560000", "0xa"),
                _snapshot("eth-updown-5m-1762560000", "0xb"),
            ]
        }
    )
    repo = FakeRepository()

    summary = await DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now)).execute()

    assert summary.new_markets == 2
    assert summary.total_markets == 2
    assert summary.skipped_slugs == 0
    assert len(repo.by_condition) == 2
    # market sedang berjalan → LIVE
    assert all(m.status is MarketStatus.LIVE for m in repo.by_condition.values())


async def test_discover_kedua_kali_tidak_menghitung_baru() -> None:
    now = datetime.fromtimestamp(1762560000, tz=UTC) + timedelta(minutes=1)
    source = FakeSource({Timeframe.FIVE_MINUTES: [_snapshot("btc-updown-5m-1762560000", "0xa")]})
    repo = FakeRepository()
    use_case = DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now))

    first = await use_case.execute()
    second = await use_case.execute()

    assert first.new_markets == 1
    assert second.new_markets == 0
    assert second.total_markets == 1
    assert len(repo.by_condition) == 1  # idempoten


async def test_slug_aneh_di_skip_tanpa_menggagalkan_batch() -> None:
    now = datetime.fromtimestamp(1762560000, tz=UTC)
    source = FakeSource(
        {
            Timeframe.FIVE_MINUTES: [
                _snapshot("bukan-slug-polinomial", "0xbad"),
                _snapshot("btc-updown-5m-1762560000", "0xa"),
                _snapshot("us-election-2026", "0xworse"),
            ]
        }
    )
    repo = FakeRepository()

    summary = await DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now)).execute()

    assert summary.new_markets == 1
    assert summary.total_markets == 1
    assert summary.skipped_slugs == 2
    assert "0xa" in repo.by_condition
    assert "0xbad" not in repo.by_condition


async def test_market_sudah_lewat_berstatus_closed() -> None:
    # slug start 1762560000, timeframe 5m → sudah lewat dari waktu sekarang
    now = datetime.fromtimestamp(1762560000, tz=UTC) + timedelta(minutes=30)
    source = FakeSource({Timeframe.FIVE_MINUTES: [_snapshot("sol-updown-5m-1762560000", "0xc")]})
    repo = FakeRepository()

    await DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now)).execute()

    assert repo.by_condition["0xc"].status is MarketStatus.CLOSED


async def test_market_15m_dan_5m_diproses() -> None:
    now = datetime.fromtimestamp(1762560000, tz=UTC) + timedelta(minutes=1)
    source = FakeSource(
        {
            Timeframe.FIVE_MINUTES: [_snapshot("btc-updown-5m-1762560000", "0xa")],
            Timeframe.FIFTEEN_MINUTES: [_snapshot("zec-updown-15m-1762560000", "0xd")],
        }
    )
    repo = FakeRepository()

    summary = await DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now)).execute()

    assert summary.total_markets == 2
    assert set(repo.by_condition) == {"0xa", "0xd"}


async def test_slug_asets_baru_terdeteksi_otomatis() -> None:
    now = datetime.fromtimestamp(1762560000, tz=UTC)
    source = FakeSource({Timeframe.FIVE_MINUTES: [_snapshot("xag-updown-5m-1762560000", "0xe")]})
    repo = FakeRepository()

    await DiscoverMarkets(source=source, repository=repo, clock=FakeClock(now)).execute()

    market = repo.by_condition["0xe"]
    assert market.slug.asset.symbol == "xag"
    assert market.slug.timeframe is Timeframe.FIVE_MINUTES
