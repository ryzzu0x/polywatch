"""Unit test use case record_result (T-009) — fake source/repo/notifier.

Jalur yang diuji: sukses (langsung resolved), retry sampai resolved,
timeout → notifikasi owner (sekali per market), idempoten, fallback sumber
harga, dan `price_source` selalu terisi.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from polywatch.application.dto.market import (
    MarketResultSnapshot,
    MarketSnapshot,
    PendingMarket,
)
from polywatch.application.use_cases.record_result import RecordResult
from polywatch.domain.market import (
    Asset,
    Market,
    MarketResult,
    MarketSlug,
    MarketStatus,
    Timeframe,
)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeMarketSource:
    """MarketSource palsu; get_result mengembalikan sekuens hasil per panggilan."""

    def __init__(self, results_per_poll: list[MarketResultSnapshot | None]) -> None:
        self._results = list(results_per_poll)
        self.get_result_calls = 0

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
        return []

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        index = min(self.get_result_calls, len(self._results) - 1)
        self.get_result_calls += 1
        return self._results[index]


class FakeRepository:
    """MarketRepository in-memory; market_results keyed by market_id.

    Method market discovery tidak dipakai use case record_result —
    implementasi kosong memenuhi protokol.
    """

    def __init__(self) -> None:
        self.results: dict[int, MarketResult] = {}
        self.statuses: dict[int, MarketStatus] = {}
        self.save_calls = 0

    async def upsert(self, market: Market) -> int:
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return []

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return []

    async def has_result(self, market_id: int) -> bool:
        return market_id in self.results

    async def save_result(self, result: MarketResult) -> None:
        self.save_calls += 1
        self.results.setdefault(result.market_id, result)  # idempoten

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        self.statuses[market_id] = status

    async def get_result(self, market_id: int) -> MarketResult | None:
        return self.results.get(market_id)


class FakePriceSource:
    def __init__(self, name: str, pair: tuple[Decimal, Decimal] | None) -> None:
        self.name = name
        self._pair = pair
        self.calls = 0

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        self.calls += 1
        return self._pair


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int | None]] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append((text, chat_id))


def _resolved_snapshot(
    slug: str, outcome_up: bool = True, volume: str = "42.068629"
) -> MarketResultSnapshot:
    from polywatch.domain.market import Outcome

    return MarketResultSnapshot(
        slug=slug,
        open_price=None,
        close_price=None,
        volume_usdc=Decimal(volume),
        resolved=True,
        outcome=Outcome.UP if outcome_up else Outcome.DOWN,
    )


def _btc_market() -> Market:
    start = datetime(2026, 11, 7, 4, 0, tzinfo=UTC)
    return Market(
        condition_id="0xabc",
        slug=MarketSlug(
            asset=Asset("btc"),
            timeframe=Timeframe.FIVE_MINUTES,
            start_at=start,
        ),
        token_up="111",
        token_down="222",
        status=MarketStatus.CLOSED,
    )


def _no_sleep(_: float) -> None:
    """Sleep instan — retry loop tidak menunggu nyata di unit test."""
    return None


async def _async_no_sleep(_: float) -> None:
    return None


async def test_sukses_resolved_langsung() -> None:
    market = _btc_market()
    source = FakeMarketSource([_resolved_snapshot("btc-updown-5m-1794024000")])
    repo = FakeRepository()
    price = FakePriceSource("polymarket-chainlink", (Decimal("75918.01"), Decimal("75920.97")))
    now = datetime(2026, 11, 7, 4, 10, tzinfo=UTC)

    use_case = RecordResult(
        source=source,
        repository=repo,
        primary_price=price,
        clock=FakeClock(now),
        sleep=_async_no_sleep,
    )
    summary = await use_case.execute_once(market, market_id=7)

    assert summary.status == "resolved"
    result = repo.results[7]
    assert result.result.value == "up"  # dari outcomePrices resolusi resmi
    assert result.open_price == Decimal("75918.01")
    assert result.close_price == Decimal("75920.97")
    assert result.volume_usdc == Decimal("42.068629")
    assert result.price_source == "polymarket-chainlink"
    assert result.resolved_at == now
    assert repo.statuses[7] is MarketStatus.RESOLVED


async def test_retry_sampai_resolved() -> None:
    """Delay resolusi ±343 s: poll None beberapa kali sebelum resolved."""
    market = _btc_market()
    slug = "btc-updown-5m-1794024000"
    source = FakeMarketSource([None, None, None, _resolved_snapshot(slug, outcome_up=False)])
    repo = FakeRepository()
    price = FakePriceSource("polymarket-chainlink", (Decimal("1"), Decimal("2")))

    use_case = RecordResult(
        source=source, repository=repo, primary_price=price, sleep=_async_no_sleep
    )
    summary = await use_case.execute_once(market, market_id=7)

    assert summary.status == "resolved"
    assert source.get_result_calls == 4  # 3x belum resolved + 1 sukses
    assert repo.results[7].result.value == "down"


async def test_timeout_memicu_notifikasi_owner_sekali() -> None:
    market = _btc_market()
    source = FakeMarketSource([None])  # tidak pernah resolved
    repo = FakeRepository()
    price = FakePriceSource("polymarket-chainlink", None)
    notifier = FakeNotifier()

    use_case = RecordResult(
        source=source,
        repository=repo,
        primary_price=price,
        notifier=notifier,
        owner_chat_id=555,
        sleep=_async_no_sleep,
        poll_interval_sec=30.0,
        timeout_sec=150.0,  # 5 interval
    )
    summary = await use_case.execute_once(market, market_id=7)

    assert summary.status == "timeout"
    assert len(notifier.sent) == 1
    text, chat_id = notifier.sent[0]
    assert "btc-updown-5m-1794024000" in text
    assert chat_id == 555
    assert 7 not in repo.results  # tidak ada baris hasil palsu
    assert source.get_result_calls == 6  # poll awal + 5 interval


async def test_timeout_notifikasi_tidak_berulang() -> None:
    market = _btc_market()
    source = FakeMarketSource([None])
    repo = FakeRepository()
    notifier = FakeNotifier()

    use_case = RecordResult(
        source=source,
        repository=repo,
        primary_price=FakePriceSource("p", None),
        notifier=notifier,
        sleep=_async_no_sleep,
        timeout_sec=0.0,  # langsung timeout tiap panggilan
    )
    await use_case.execute_once(market, market_id=7)
    await use_case.execute_once(market, market_id=7)

    assert len(notifier.sent) == 1  # maksimal sekali per market


async def test_idempoten_tidak_duplikat() -> None:
    market = _btc_market()
    snapshot = _resolved_snapshot("btc-updown-5m-1794024000")
    source = FakeMarketSource([snapshot])
    repo = FakeRepository()
    price = FakePriceSource("polymarket-chainlink", (Decimal("1"), Decimal("2")))

    use_case = RecordResult(
        source=source, repository=repo, primary_price=price, sleep=_async_no_sleep
    )
    await use_case.execute_once(market, market_id=7)
    await use_case.execute_once(market, market_id=7)

    assert repo.save_calls == 1  # hanya disimpan sekali
    assert len(repo.results) == 1


async def test_fallback_sumber_harga_dipakai_bila_utama_none() -> None:
    market = _btc_market()
    source = FakeMarketSource([_resolved_snapshot("btc-updown-5m-1794024000")])
    repo = FakeRepository()
    primary = FakePriceSource("polymarket-chainlink", None)  # window sudah bergeser
    fallback = FakePriceSource("binance-1m", (Decimal("100"), Decimal("101")))

    use_case = RecordResult(
        source=source,
        repository=repo,
        primary_price=primary,
        fallback_price=fallback,
        sleep=_async_no_sleep,
    )
    await use_case.execute_once(market, market_id=7)

    assert repo.results[7].price_source == "binance-1m"
    assert repo.results[7].open_price == Decimal("100")
    assert fallback.calls == 1


async def test_price_source_selalu_terisi_bila_semua_gagal() -> None:
    market = _btc_market()
    source = FakeMarketSource([_resolved_snapshot("btc-updown-5m-1794024000")])
    repo = FakeRepository()
    primary = FakePriceSource("polymarket-chainlink", None)
    fallback = FakePriceSource("binance-1m", None)

    use_case = RecordResult(
        source=source,
        repository=repo,
        primary_price=primary,
        fallback_price=fallback,
        sleep=_async_no_sleep,
    )
    await use_case.execute_once(market, market_id=7)

    result = repo.results[7]
    assert result.price_source == "unavailable"
    assert result.result.value == "up"  # arah tetap dari resolusi resmi
