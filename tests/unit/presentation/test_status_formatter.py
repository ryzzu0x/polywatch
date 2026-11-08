"""Unit test formatter /status dan use case BotStatus (T-014) — fake repo.

Fake repository hanya mengimplementasikan method yang dipakai BotStatus;
kesesuaian penuh dengan port dijamin mypy lewat cast ke Protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from polywatch import __version__
from polywatch.application.ports import (
    MarketStatsRepository,
    TradeStatsRepository,
)
from polywatch.application.use_cases.bot_status import BotStatus
from polywatch.domain.market import MarketSlug, MarketStatus


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class _FakeMarket:
    def __init__(self, slug: str) -> None:
        self.slug = MarketSlug.parse(slug)


class FakeMarketRepo:
    """Fake repository: data ditentukan di konstruktor."""

    def __init__(self, live_slugs: list[str], counts: dict[str, int]) -> None:
        self._live_slugs = live_slugs
        self._counts = counts

    async def list_by_status(self, status: MarketStatus) -> list[_FakeMarket]:
        if status is not MarketStatus.LIVE:
            return []
        return [_FakeMarket(s) for s in self._live_slugs]

    async def count_by_status(self, status: MarketStatus) -> int:
        return self._counts.get(status.value, 0)


class FakeTradeRepo:
    def __init__(self, count: int) -> None:
        self._count = count
        self.last_since: datetime | None = None

    async def count_since(self, since: datetime) -> int:
        self.last_since = since
        return self._count


def _markets(live: list[str], counts: dict[str, int]) -> MarketStatsRepository:
    return cast(MarketStatsRepository, FakeMarketRepo(live, counts))


def _trades(count: int) -> TradeStatsRepository:
    return cast(TradeStatsRepository, FakeTradeRepo(count))


async def test_status_menyertakan_versi_dan_ringkasan() -> None:
    markets = _markets(
        ["btc-updown-5m-1794110400"], {"live": 2, "resolved": 10, "upcoming": 1, "closed": 3}
    )
    trades = FakeTradeRepo(57)
    use_case = BotStatus(
        markets, cast(TradeStatsRepository, trades), FakeClock(), ["ws:connected", "rpc:ok"]
    )

    text = await use_case.execute()

    assert "Status polywatch:" in text
    assert __version__ in text
    assert "live: 2" in text
    assert "resolved: 10" in text
    assert "Trade 24 jam terakhir: 57" in text
    assert "ws=connected" in text
    assert "rpc=ok" in text


async def test_status_tanpa_market_live_dan_tanpa_stream() -> None:
    use_case = BotStatus(_markets([], {}), _trades(0), FakeClock())
    text = await use_case.execute()
    assert "tidak ada" in text
    assert "Stream:" not in text


async def test_status_trade_24jam_dihitung_dari_clock() -> None:
    clock = FakeClock()
    trades = FakeTradeRepo(1)
    use_case = BotStatus(_markets([], {}), cast(TradeStatsRepository, trades), clock)
    await use_case.execute()
    assert trades.last_since is not None
    delta = (clock.now() - trades.last_since).total_seconds()
    assert 24 * 3600 - 1 <= delta <= 24 * 3600 + 1


async def test_status_parses_stream_status_entri_terakhir_menang() -> None:
    use_case = BotStatus(
        _markets([], {}),
        _trades(0),
        FakeClock(),
        ["ws:disconnected", "ws:connected", "rpc:failover"],
    )
    snap = await use_case.snapshot()
    assert snap.ws_status == "connected"
    assert snap.rpc_status == "failover"
