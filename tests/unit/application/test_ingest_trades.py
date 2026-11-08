"""Unit test use case ingest_trades: dedupe, mapping token, idempoten, backfill."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polywatch.application.dto.market import PendingMarket, TokenMarketInfo
from polywatch.application.dto.trade import (
    FilledOrderSnapshot,
    ResolvedTrade,
    TradeEventSnapshot,
    TradeSnapshot,
)
from polywatch.application.use_cases.ingest_trades import EXCHANGE_ADDRESS, IngestTrades
from polywatch.domain.market import (
    Market,
    MarketResult,
    MarketStatus,
    Outcome,
    Timeframe,
)
from polywatch.domain.trade import Action, Trade

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

MARKET = TokenMarketInfo(
    market_id=7,
    condition_id="0xcid",
    token_up="111",
    token_down="222",
    slug="btc-updown-5m-1000",
)


class FakeClock:
    def __init__(self) -> None:
        self._now = NOW

    def now(self) -> datetime:
        return self._now


class FakeStream:
    """Yield event dari daftar lalu berhenti (stream selesai)."""

    def __init__(self, events: Sequence[TradeEventSnapshot]) -> None:
        self._events = list(events)
        self.subscribed: list[str] | None = None

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.subscribed = list(token_ids)
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        for event in self._events:
            yield event


class FakeReceipts:
    """Fill per tx_hash; None berarti receipt gagal/timeout."""

    def __init__(self, fills: Mapping[str, Sequence[FilledOrderSnapshot] | None]) -> None:
        self._fills = fills
        self.calls: list[str] = []

    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot] | None:
        self.calls.append(tx_hash)
        return self._fills.get(tx_hash, [])


class FakeTradeRepository:
    """Repository in-memory dengan kunci idempoten sama seperti DB."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], Trade] = {}
        self.insert_calls: list[Sequence[Trade]] = []

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        self.insert_calls.append(list(trades))
        new = 0
        for trade in trades:
            key = (trade.tx_hash, trade.log_index, trade.wallet)
            if key not in self.rows:
                self.rows[key] = trade
                new += 1
        return new

    async def last_traded_at(self, market_id: int) -> datetime | None:
        return None

    async def wallets_for_market(self, market_id: int) -> Sequence[str]:
        return sorted({t.wallet for t in self.rows.values() if t.market_id == market_id})

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        return []


class FakeMarketRepository:
    def __init__(self, markets: dict[int, TokenMarketInfo]) -> None:
        self._markets = markets

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return self._markets.get(market_id)

    async def upsert(self, market: Market) -> int:
        raise NotImplementedError

    async def get_by_slug(self, slug: str) -> None:
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return []

    async def get_result(self, market_id: int) -> MarketResult | None:
        return None

    async def save_result(self, result: MarketResult) -> None:
        raise NotImplementedError

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        raise NotImplementedError

    async def has_result(self, market_id: int) -> bool:
        return False

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return []


class FakeDataApi:
    def __init__(self, snapshots: Sequence[TradeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str] = []

    async def fetch_trades(
        self, condition_id: str, since: datetime | None = None
    ) -> Sequence[TradeSnapshot]:
        self.calls.append(condition_id)
        return self._snapshots


def _event(tx: str) -> TradeEventSnapshot:
    return TradeEventSnapshot(
        tx_hash=tx, asset_id="111", condition_id="0xcid", timestamp_ms=1794122884052
    )


def _fill(
    tx: str, maker: str, taker: str, side: int, token_id: int, log_index: int = 1
) -> FilledOrderSnapshot:
    return FilledOrderSnapshot(
        tx_hash=tx,
        log_index=log_index,
        maker=maker,
        taker=taker,
        side=side,
        token_id=token_id,
        size=Decimal("8.333334"),
        usdc=Decimal("1"),
        fee=0,
    )


WALLET_A = "0x" + "ab" * 20
WALLET_B = "0x" + "cd" * 20


def _make_ingest(
    events: Sequence[TradeEventSnapshot],
    fills: Mapping[str, Sequence[FilledOrderSnapshot] | None],
    repo: FakeTradeRepository | None = None,
    on_saved: object | None = None,
    data_api: FakeDataApi | None = None,
) -> tuple[IngestTrades, FakeStream, FakeReceipts, FakeTradeRepository]:
    repo = repo or FakeTradeRepository()
    stream = FakeStream(events)
    receipts = FakeReceipts(fills)
    ingest = IngestTrades(
        stream=stream,
        receipts=receipts,
        trades=repo,
        markets=FakeMarketRepository({7: MARKET}),
        token_markets=[MARKET],
        clock=FakeClock(),
        on_saved=on_saved,  # type: ignore[arg-type]
        data_api=data_api,
    )
    return ingest, stream, receipts, repo


async def test_ingest_simpan_maker_dan_taker() -> None:
    ingest, _, _, repo = _make_ingest(
        [_event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]},
    )
    stats = await ingest.run_once()
    assert stats.trades_saved == 2
    wallets = {t.wallet for t in repo.rows.values()}
    assert wallets == {WALLET_A, WALLET_B}


async def test_ingest_taker_exchange_hanya_maker() -> None:
    ingest, _, _, repo = _make_ingest(
        [_event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, EXCHANGE_ADDRESS, side=1, token_id=222)]},
    )
    await ingest.run_once()
    assert len(repo.rows) == 1
    trade = next(iter(repo.rows.values()))
    assert trade.wallet == WALLET_A
    assert trade.outcome is Outcome.DOWN
    assert trade.action is Action.SELL
    assert trade.size == Decimal("8.333334")
    assert trade.usdc == Decimal("1")


async def test_ingest_dedupe_tx_hash_duplikat() -> None:
    ingest, _, receipts, repo = _make_ingest(
        [_event("0xtx1"), _event("0xtx1"), _event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]},
    )
    stats = await ingest.run_once()
    assert stats.events_seen == 3
    assert stats.tx_deduped == 2
    assert len(receipts.calls) == 1  # fetch receipt hanya sekali
    assert len(repo.rows) == 2


async def test_ingest_idempoten_insert_ulang() -> None:
    fills = {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]}
    repo = FakeTradeRepository()
    ingest, _, _, _ = _make_ingest([_event("0xtx1")], fills, repo=repo)
    await ingest.run_once()
    # jalankan ulang (cache tx direset — simulasi restart) → insert_many
    # dengan kunci sama → tidak ada baris baru (ON CONFLICT DO NOTHING)
    saved = await repo.insert_many(list(repo.rows.values()))
    assert saved == 0
    assert len(repo.rows) == 2


async def test_ingest_fill_token_tidak_dipantau_diabaikan() -> None:
    ingest, _, _, repo = _make_ingest(
        [_event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=999)]},
    )
    stats = await ingest.run_once()
    assert stats.trades_saved == 0
    assert repo.rows == {}


async def test_ingest_receipt_gagal_dihitung() -> None:
    ingest, _, _, repo = _make_ingest([_event("0xtx1")], {"0xtx1": None})
    stats = await ingest.run_once()
    assert stats.receipts_failed == 1
    assert repo.rows == {}


async def test_ingest_on_saved_dipanggil_bila_ada_baris() -> None:
    saved_batches: list[Sequence[Trade]] = []

    async def on_saved(trades: Sequence[Trade]) -> None:
        saved_batches.append(trades)

    ingest, _, _, _ = _make_ingest(
        [_event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]},
        on_saved=on_saved,
    )
    await ingest.run_once()
    assert len(saved_batches) == 1
    assert len(saved_batches[0]) == 2


async def test_ingest_tanpa_token_kosong() -> None:
    ingest, stream, _, repo = _make_ingest([_event("0xtx1")], {"0xtx1": []})
    ingest._token_markets = []
    stats = await ingest.run_once()
    assert stats.events_seen == 0
    assert stream.subscribed is None
    assert repo.rows == {}


async def test_ingest_price_dihitung_dari_usdc_per_size() -> None:
    ingest, _, _, repo = _make_ingest(
        [_event("0xtx1")],
        {"0xtx1": [_fill("0xtx1", WALLET_A, EXCHANGE_ADDRESS, side=0, token_id=111)]},
    )
    await ingest.run_once()
    trade = next(iter(repo.rows.values()))
    assert trade.price == Decimal("1") / Decimal("8.333334")


async def test_backfill_lewat_data_api_idempoten() -> None:
    snapshot = TradeSnapshot(
        tx_hash="0xtx1",
        log_index=5,
        wallet=WALLET_A,
        outcome=Outcome.UP,
        action=Action.BUY,
        price=Decimal("0.5"),
        size=Decimal("10"),
        usdc=Decimal("5"),
        traded_at=NOW,
    )
    api = FakeDataApi([snapshot, snapshot])
    repo = FakeTradeRepository()
    ingest, _, _, _ = _make_ingest([], {}, repo=repo, data_api=api)
    first = await ingest.backfill(7)
    second = await ingest.backfill(7)
    assert first == 1
    assert second == 0  # idempoten
    assert api.calls == ["0xcid", "0xcid"]
    assert len(repo.rows) == 1


async def test_backfill_tanpa_data_api_raise() -> None:
    ingest, _, _, _ = _make_ingest([], {})
    with pytest.raises(RuntimeError, match="Data API"):
        await ingest.backfill(7)


async def test_backfill_market_tidak_ada() -> None:
    ingest, _, _, _ = _make_ingest([], {}, data_api=FakeDataApi([]))
    assert await ingest.backfill(999) == 0


# ------------------------------------------------------- T-026: loop kontinyu


class FakeLiveTokens:
    """LiveTokenSource palsu: daftar berganti antar panggilan."""

    def __init__(self, batches: Sequence[Sequence[TokenMarketInfo]]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return batch


class RestartableFakeStream:
    """Stream palsu untuk `run()`: tiap panggilan `stream()` menghasilkan
    satu event lalu berakhir (mensimulasikan close 1000 tiap jendela)."""

    def __init__(self, events_per_cycle: Sequence[TradeEventSnapshot]) -> None:
        self._events = list(events_per_cycle)
        self.stream_calls: list[list[str]] = []

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.stream_calls.append(list(token_ids))
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        for event in self._events:
            yield event


async def _sleep_noop(_: float) -> None:
    return None


async def test_run_loop_kontinyu_refresh_token_tiap_siklus() -> None:
    """T-026: `run()` mempertahankan loop — stream berakhir lalu dilanjutkan
    dengan token live baru (bukan mati setelah siklus pertama)."""
    stream = RestartableFakeStream([_event("0xtx1")])
    fills = {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]}
    repo = FakeTradeRepository()
    live = FakeLiveTokens([[MARKET], [MARKET], [MARKET]])
    stop = asyncio.Event()

    ingest = IngestTrades(
        stream=stream,
        receipts=FakeReceipts(fills),
        trades=repo,
        markets=FakeMarketRepository({7: MARKET}),
        clock=FakeClock(),
        live_tokens=live,
        live_refresh_sec=0.01,
        sleep=_sleep_noop,
    )

    async def _stop_after_three_cycles() -> None:
        for _ in range(500):
            if len(stream.stream_calls) >= 3:
                break
            await asyncio.sleep(0.001)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_after_three_cycles())

    # tiga siklus stream — langganan tidak mati setelah siklus pertama
    assert len(stream.stream_calls) == 3
    assert stream.stream_calls[0] == ["111", "222"]
    # fill maker+taker → 2 trade; siklus 2-3 event sama → dedupe cache
    # in-memory (idempoten DB juga menjaga bila cache terpotong)
    assert len(repo.rows) == 2
    assert live.calls >= 3


async def test_run_loop_tanpa_token_tunggu_lalu_lanjut() -> None:
    """T-026: tanpa token live → tunggu interval, tidak crash."""
    stream = RestartableFakeStream([])
    ingest = IngestTrades(
        stream=stream,
        receipts=FakeReceipts({}),
        trades=FakeTradeRepository(),
        markets=FakeMarketRepository({}),
        clock=FakeClock(),
        live_tokens=FakeLiveTokens([[]]),
        live_refresh_sec=0.01,
        sleep=_sleep_noop,
    )
    stop = asyncio.Event()

    async def _stop_soon() -> None:
        for _ in range(50):
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_soon())
    assert stream.stream_calls == []  # tidak pernah subscribe tanpa token


async def test_run_loop_gagal_query_token_tetap_hidup() -> None:
    """T-026: live_tokens melempar → pakai daftar sebelumnya, loop jalan."""

    class FailingLiveTokens(FakeLiveTokens):
        async def live_tokens(
            self, timeframes: Sequence[Timeframe] | None = None
        ) -> Sequence[TokenMarketInfo]:
            self.calls += 1
            if self.calls == 1:
                return [MARKET]
            raise RuntimeError("db down")

    stream = RestartableFakeStream([_event("0xtx1")])
    ingest = IngestTrades(
        stream=stream,
        receipts=FakeReceipts(
            {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]}
        ),
        trades=FakeTradeRepository(),
        markets=FakeMarketRepository({7: MARKET}),
        clock=FakeClock(),
        live_tokens=FailingLiveTokens([]),
        live_refresh_sec=0.01,
        sleep=_sleep_noop,
    )
    stop = asyncio.Event()

    async def _stop_after_two() -> None:
        for _ in range(500):
            if len(stream.stream_calls) >= 2:
                break
            await asyncio.sleep(0.001)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_after_two())
    # kegagalan query kedua tidak mematikan loop — siklus tetap lanjut
    assert len(stream.stream_calls) == 2


async def test_run_loop_commit_per_batch() -> None:
    """T-026: on_batch_commit dipanggil tiap batch tersimpan — stream
    kontinyu tidak pernah berakhir, commit tidak boleh menunggu akhir siklus."""
    stream = RestartableFakeStream([_event("0xtx1")])
    commits: list[int] = []

    async def _commit_counter() -> None:
        commits.append(1)

    ingest = IngestTrades(
        stream=stream,
        receipts=FakeReceipts(
            {"0xtx1": [_fill("0xtx1", WALLET_A, WALLET_B, side=0, token_id=111)]}
        ),
        trades=FakeTradeRepository(),
        markets=FakeMarketRepository({7: MARKET}),
        clock=FakeClock(),
        token_markets=[MARKET],
        on_batch_commit=_commit_counter,
        live_refresh_sec=0.01,
        sleep=_sleep_noop,
    )

    # hentikan setelah stream pertama berakhir (event tunggal tersimpan)
    stop = asyncio.Event()

    async def _stop_after_first() -> None:
        for _ in range(500):
            if len(stream.stream_calls) >= 1 and commits:
                break
            await asyncio.sleep(0.001)
        stop.set()

    # run() dengan token statis (tanpa live_tokens) — satu siklus lalu stop
    await asyncio.gather(ingest.run(stop), _stop_after_first())
    assert commits == [1]  # batch tersimpan → commit langsung, bukan di akhir
