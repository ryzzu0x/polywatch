"""Unit test hook on_saved ingest_trades → evaluate_alert (T-017).

Membuktikan: (a) trade tersimpan memicu evaluasi alert lewat hook, (b)
tanpa hook perilaku ingest tidak berubah (test lama tetap lulus).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.dto.trade import FilledOrderSnapshot, TradeEventSnapshot
from polywatch.application.ports import MarketRepository, TradeRepository
from polywatch.application.use_cases.ingest_trades import IngestTrades, OnSavedCallback
from polywatch.domain.trade import Trade

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
WALLET_A = "0x" + "ab" * 20
WALLET_B = "0x" + "cd" * 20

MARKET = TokenMarketInfo(
    market_id=7,
    condition_id="0xcid",
    token_up="111",
    token_down="222",
    slug="btc-updown-5m-1000",
)


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeStream:
    def __init__(self, events: list[TradeEventSnapshot]) -> None:
        self._events = events

    def stream(self, token_ids: list[str]) -> AsyncIterator[TradeEventSnapshot]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        for event in self._events:
            yield event


class FakeReceipts:
    def __init__(self, fills: dict[str, list[FilledOrderSnapshot]]) -> None:
        self._fills = fills

    async def filled_orders(self, tx_hash: str) -> list[FilledOrderSnapshot] | None:
        return self._fills.get(tx_hash, [])


class FakeTradeRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], Trade] = {}

    async def insert_many(self, trades: list[Trade]) -> int:
        new = 0
        for trade in trades:
            key = (trade.tx_hash, trade.log_index, trade.wallet)
            if key not in self.rows:
                self.rows[key] = trade
                new += 1
        return new


class FakeMarketRepository:
    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return MARKET if market_id == MARKET.market_id else None


def _make_ingest(
    on_saved: OnSavedCallback | None = None,
) -> tuple[IngestTrades, FakeTradeRepository]:
    repo = FakeTradeRepository()
    fill = FilledOrderSnapshot(
        tx_hash="0xtx1",
        log_index=1,
        maker=WALLET_A,
        taker=WALLET_B,
        side=0,
        token_id=111,
        size=Decimal("833.33"),
        usdc=Decimal("500"),
        fee=0,
    )
    ingest = IngestTrades(
        stream=FakeStream(  # type: ignore[arg-type]
            [
                TradeEventSnapshot(
                    tx_hash="0xtx1", asset_id="111", condition_id="0xcid", timestamp_ms=1
                )
            ]
        ),
        receipts=cast(Any, FakeReceipts({"0xtx1": [fill]})),
        trades=cast(TradeRepository, repo),
        markets=cast(MarketRepository, FakeMarketRepository()),
        token_markets=[MARKET],
        clock=FakeClock(),
        on_saved=on_saved,
    )
    return ingest, repo


async def test_hook_dipanggil_dengan_trade_baru() -> None:
    received: list[Trade] = []

    async def on_saved(trades: Sequence[Trade]) -> None:
        received.extend(trades)

    ingest, _repo = _make_ingest(on_saved)
    stats = await ingest.run_once()
    assert stats.trades_saved == 2
    assert len(received) == 2
    assert {t.wallet for t in received} == {WALLET_A, WALLET_B}


async def test_tanpa_hook_perilaku_tidak_berubah() -> None:
    ingest, repo = _make_ingest(on_saved=None)
    stats = await ingest.run_once()
    assert stats.trades_saved == 2
    assert len(repo.rows) == 2


async def test_hook_error_tidak_menghentikan_ingest() -> None:
    """Pemanggil (container) bertanggung jawab menangkap error hook —
    ingest sendiri meneruskan exception; container membungkusnya try/except.
    Test ini memastikan kontrak itu eksplisit.
    """

    async def on_saved(trades: Sequence[Trade]) -> None:
        raise RuntimeError("boom")

    ingest, repo = _make_ingest(on_saved)
    with pytest.raises(RuntimeError, match="boom"):
        await ingest.run_once()
    # trade tetap tersimpan sebelum hook dipanggil
    assert len(repo.rows) == 2
