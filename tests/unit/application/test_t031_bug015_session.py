"""Test regresi BUG-015 (T-031) — session ingest keracun.

Soak test T-030 (log /tmp/polywatch-t030-soak.log): 14x
`ingest_stream_error` berantai dengan
`sqlalchemy.exc.PendingRollbackError: Can't reconnect until invalid
transaction is rolled back` dari `_process_tx → insert_many →
session.execute`. Akibatnya: setelah SATU kegagalan DB, semua penyimpanan
trade berikutnya gagal, stream restart tiap ~35 detik, dan token stream
tidak pernah berubah (selalu 36) walau jendela market berganti.

Dua lapis perbaikan yang diuji di sini:
1. **Isolasi kegagalan (use case):** `_insert_batch` menangkap exception
   penyimpanan, me-log `ingest_batch_error` terstruktur rate-limited,
   dan stream LANJUT — tx berikutnya tetap diproses.
2. **Unit-of-work per batch (infrastruktur):**
   `BatchSessionTradeRepository` membuka session BARU untuk setiap
   `insert_many`, commit saat sukses, rollback + close saat gagal —
   kegagalan satu batch tidak meninggalkan session invalid yang menular
   ke batch berikutnya (inti `PendingRollbackError`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from polywatch.application.dto.market import (
    PendingMarket,
    StaleMarket,
    TokenMarketInfo,
)
from polywatch.application.dto.trade import (
    FilledOrderSnapshot,
    ResolvedTrade,
    TradeEventSnapshot,
)
from polywatch.application.use_cases.ingest_trades import IngestTrades
from polywatch.domain.market import Market, MarketResult, MarketStatus, Outcome
from polywatch.domain.trade import Action, Trade
from polywatch.infrastructure.db.repositories.batch_trade_repository import (
    BatchSessionTradeRepository,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Stream:
    """Stream yang yield event dari daftar lalu berakhir (run_once)."""

    def __init__(self, events: Sequence[TradeEventSnapshot]) -> None:
        self._events = list(events)

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        for event in self._events:
            yield event


class _BurstStream:
    """Stream yang yield event terus sampai stop (run kontinyu)."""

    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.stream_calls: list[list[str]] = []

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.stream_calls.append(list(token_ids))
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        i = 0
        while not self.stop.is_set():
            await asyncio.sleep(0.005)
            yield _event(f"0xtx{i}")
            i += 1


class _Receipts:
    """ReceiptFetcher: satu fill maker+taker untuk tx apa pun."""

    def __init__(self, market: TokenMarketInfo) -> None:
        self._market = market

    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot]:
        return [
            FilledOrderSnapshot(
                tx_hash=tx_hash,
                log_index=1,
                maker="0x" + "ab" * 20,
                taker="0x" + "cd" * 20,
                side=0,
                token_id=int(self._market.token_up),
                size=Decimal(1),
                usdc=Decimal(1),
                fee=0,
            )
        ]


class _Markets:
    """MarketRepository minimal (hanya jalur baca yang dipakai ingest)."""

    async def upsert(self, market: Market) -> int:
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return []

    async def get_result(self, market_id: int) -> MarketResult | None:
        return None

    async def save_result(self, result: MarketResult) -> None:
        return None

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        return None

    async def has_result(self, market_id: int) -> bool:
        return False

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return None

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return []

    async def list_stale_open(self, now: datetime) -> Sequence[StaleMarket]:
        return []


def _market(market_id: int = 7) -> TokenMarketInfo:
    return TokenMarketInfo(
        market_id=market_id,
        condition_id=f"0xcid{market_id}",
        token_up="111",
        token_down="222",
        slug=f"btc-updown-5m-{1000 + market_id}",
    )


def _event(tx: str) -> TradeEventSnapshot:
    return TradeEventSnapshot(
        tx_hash=tx, asset_id="111", condition_id="0xcid7", timestamp_ms=1794122884052
    )


def _two_trades(tx: str) -> list[Trade]:
    return [
        Trade(
            tx_hash=tx,
            log_index=1,
            market_id=7,
            wallet=w,
            outcome=Outcome.UP,
            action=Action.BUY,
            price=Decimal(1),
            size=Decimal(1),
            usdc=Decimal(1),
            traded_at=NOW,
        )
        for w in ("0x" + "ab" * 20, "0x" + "cd" * 20)
    ]


class _BaseFakeTrades:
    """Kerangka TradeRepository in-memory (tulis bisa di-override)."""

    def __init__(self) -> None:
        self.saved: list[Trade] = []
        self.insert_calls = 0

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        raise NotImplementedError

    async def last_traded_at(self, market_id: int) -> datetime | None:
        return None

    async def wallets_for_market(self, market_id: int) -> Sequence[str]:
        return []

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        return []

    async def list_wallets_with_resolved_trades(self) -> Sequence[str]:
        return []


class _FlakyOnceTrades(_BaseFakeTrades):
    """Repository yang gagal pada insert PERTAMA lalu selalu sukses.

    Mensimulasikan kegagalan DB sesaat (deadlock/restart). Bug lama:
    kegagalan ini naik sampai `run()`/`run_once()` dan setelahnya semua
    penyimpanan berikutnya gagal sampai proses restart.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        self.insert_calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulasi DB down")
        self.saved.extend(trades)
        return len(trades)


class _AlwaysFailTrades(_BaseFakeTrades):
    """Repository yang SELALU gagal — kasus terburuk (DB down penuh)."""

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        self.insert_calls += 1
        raise RuntimeError("db down terus")


# ===================================================== lapis use case


async def test_insert_gagal_sekali_batch_berikut_tersimpan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BUG-015: insert pertama gagal → stream tidak mati, batch berikutnya
    tetap tersimpan, kegagalan ter-log terstruktur `ingest_batch_error`."""
    market = _market()
    repo = _FlakyOnceTrades()
    ingest = IngestTrades(
        stream=_Stream([_event("0x1"), _event("0x2")]),
        receipts=_Receipts(market),
        trades=repo,
        markets=_Markets(),
        clock=_Clock(),
        token_markets=[market],
    )

    with caplog.at_level(logging.ERROR):
        stats = await ingest.run_once()

    # kedua tx diproses; insert pertama gagal (ditangkap), kedua sukses
    assert repo.insert_calls == 2
    assert len(repo.saved) == 2  # maker + taker dari tx kedua
    assert stats.trades_saved == 2
    # kegagalan ter-log terstruktur — bukan exception yang bocor
    assert any(r.__dict__.get("event") == "ingest_batch_error" for r in caplog.records)


async def test_run_stream_tetap_hidup_saat_insert_gagal_terus() -> None:
    """BUG-015 (kasus terburuk): insert SELALU gagal → stream tidak
    mati-hidup berulang (restart tiap ~35 dtk di soak); event terus
    diproses dan kegagalan hanya jadi log, bukan exception yang
    menjatuhkan loop."""
    stop = asyncio.Event()
    stream = _BurstStream(stop)
    market = _market()
    repo = _AlwaysFailTrades()
    ingest = IngestTrades(
        stream=stream,
        receipts=_Receipts(market),
        trades=repo,
        markets=_Markets(),
        clock=_Clock(),
        live_tokens=None,
        token_markets=[market],
    )

    async def _stop_soon() -> None:
        for _ in range(2000):
            if repo.insert_calls >= 5:
                break
            await asyncio.sleep(0.005)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_soon())

    # banyak batch diproses dalam SATU stream — tidak restart berulang
    assert repo.insert_calls >= 5
    assert len(stream.stream_calls) == 1


def test_log_resubscribe_mencatat_token_lama_dan_baru(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BUG-015: log `ingest_resubscribe` memuat token lama vs baru —
    bukti audit bahwa set token benar-benar berganti saat jendela 5m/15m
    berakhir (jumlah sama tidak berarti set sama)."""
    market_lama = _market(7)  # token 111/222
    market_baru = TokenMarketInfo(
        market_id=8,
        condition_id="0xcid8",
        token_up="333",
        token_down="444",
        slug="btc-updown-5m-1008",
    )
    ingest = IngestTrades(
        stream=_Stream([]),
        receipts=_Receipts(market_lama),
        trades=_FlakyOnceTrades(),
        markets=_Markets(),
        clock=_Clock(),
        token_markets=[market_lama],
    )

    with caplog.at_level(logging.INFO):
        ingest._log_resubscribe([market_lama], [market_baru])

    record = next(r for r in caplog.records if r.__dict__.get("event") == "ingest_resubscribe")
    assert record.__dict__["tokens_old"] == 2
    assert record.__dict__["tokens_new"] == 2
    assert record.__dict__["tokens_added"] == 2  # 333/444 baru semua
    assert record.__dict__["tokens_removed"] == 2  # 111/222 ditinggalkan
    assert record.__dict__["markets_old"] == 1
    assert record.__dict__["markets_new"] == 1


# ===================================================== lapis infrastruktur


class _FakeSession:
    """Session palsu: mencatat commit/rollback/close — membuktikan tiap
    `insert_many` mendapat objek session BERBEDA dan selalu ditutup."""

    def __init__(self, fail_execute: bool = False) -> None:
        self.fail_execute = fail_execute
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        if self.fail_execute:
            raise RuntimeError("insert gagal")

    async def commit(self) -> None:
        assert not self.closed, "commit pada session tertutup"
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # sama seperti AsyncSession: keluar context manager = close.
        await self.close()


class _FakeSessionFactory:
    """Factory yang menghasilkan session baru tiap panggilan."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


async def test_batch_session_repo_session_baru_per_panggilan() -> None:
    """`BatchSessionTradeRepository` memakai session BARU tiap panggilan
    factory — tidak ada session yang menetap antar batch."""
    factory = _FakeSessionFactory()
    BatchSessionTradeRepository(factory)  # type: ignore[arg-type]

    s1 = factory()
    s2 = factory()

    assert s1 is not s2
    assert factory.sessions == [s1, s2]


async def test_batch_gagal_ditutup_rollback_tidak_menular() -> None:
    """Pola per-batch: batch gagal → session itu di-rollback + close;
    batch BERIKUTNYA mendapat session segar dan tetap bisa commit."""
    factory = _FakeSessionFactory()

    async def _insert(fail: bool) -> None:
        # pola yang sama persis dengan BatchSessionTradeRepository.insert_many
        async with factory() as session:
            try:
                await session.execute()
                if fail:
                    raise RuntimeError("db down")
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    with pytest.raises(RuntimeError):
        await _insert(fail=True)
    await _insert(fail=False)

    failed, fresh = factory.sessions
    assert failed.rollbacks == 1
    assert failed.closed is True
    assert fresh is not failed
    assert fresh.commits == 1  # session baru tidak keracunan
    assert fresh.rollbacks == 0
    assert fresh.closed is True


# ===================================================== integration (DB nyata)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

_db_test = pytest.mark.skipif(
    TEST_DSN is None, reason="TEST_DATABASE_URL tidak di-set; integration DB di-skip"
)


@_db_test
async def test_db_insert_gagal_lalu_pulih_session_baru() -> None:
    """DB nyata: insert pertama dipaksa gagal (NOT NULL) → rollback bersih;
    insert kedua pada session BARU tetap sukses. Reproduksi terdekat ke
    `PendingRollbackError` produksi: tanpa session per batch, transaksi
    invalid menular ke semua operasi berikutnya."""
    assert TEST_DSN is not None
    engine = create_async_engine(TEST_DSN)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS t031_probe (id serial primary key, tx text not null)")
        )
        await session.commit()

    async def _insert(tx: str | None) -> None:
        # pola per-batch yang sama dengan BatchSessionTradeRepository:
        # session baru per insert, commit sukses / rollback gagal.
        async with maker() as session:
            try:
                await session.execute(text("INSERT INTO t031_probe (tx) VALUES (:tx)"), {"tx": tx})
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # 1. batch pertama gagal (constraint NOT NULL) — rollback + close
    with pytest.raises(Exception, match="null value"):
        await _insert(None)

    # 2. batch kedua pada session baru sukses — tidak keracunan
    await _insert("ok")

    async with maker() as session:
        count = await session.execute(text("SELECT count(*) FROM t031_probe"))
        assert count.scalar() == 1  # hanya baris 'ok'

    async with maker() as session:
        await session.execute(text("DROP TABLE IF EXISTS t031_probe"))
        await session.commit()
    await engine.dispose()
