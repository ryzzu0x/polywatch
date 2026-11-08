"""E2E core flow (T-018): market → trade → alert → resolve → skor.

DB nyata `polywatch_test` + semua sumber eksternal palsu (tanpa jaringan):
- fake `MarketSource` (list_markets + get_result) untuk discover & resolve,
- fake WS + fake RPC dengan receipt fixture nyata dari `docs/spikes/`
  (via `tests/fixtures/t005c_receipt_orderfilled.json`) untuk ingest,
- fake `PriceSource` + fake `Notifier` untuk record_result & alert.

Bersih-bersih sendiri: truncate tabel yang dipakai sebelum & sesudah test.
Auto-skip bila `TEST_DATABASE_URL` tidak di-set (sama seperti integration).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.application.dto.market import (
    MarketResultSnapshot,
    MarketSnapshot,
    TokenMarketInfo,
)
from polywatch.application.dto.trade import (
    FilledOrderSnapshot,
    TradeEventSnapshot,
)
from polywatch.application.ports import (
    AlertSettingsRepository,
    AlertsSentRepository,
    MarketRepository,
    MarketSource,
    Notifier,
    PriceSource,
    ReceiptFetcher,
    TradeRepository,
    WatchlistRepository,
)
from polywatch.application.use_cases.discover_markets import DiscoverMarkets
from polywatch.application.use_cases.evaluate_alert import EvaluateAlert
from polywatch.application.use_cases.ingest_trades import IngestTrades
from polywatch.application.use_cases.recompute_scores import RecomputeScores
from polywatch.application.use_cases.record_result import RecordResult, RecordResultSummary
from polywatch.domain.market import MarketStatus, Outcome, Timeframe
from polywatch.domain.scoring import (
    ScoringPolicy,
    WalletStats,
    aggregate_market,
    build_wallet_stats,
)
from polywatch.domain.trade import Trade
from polywatch.infrastructure.db.models import (
    AlertSentModel,
    AssetModel,
    MarketModel,
    TradeModel,
)
from polywatch.infrastructure.db.repositories.alert_repository import (
    SqlAlchemyAlertSettingsRepository,
    SqlAlchemyAlertsSentRepository,
)
from polywatch.infrastructure.db.repositories.market_repository import (
    SqlAlchemyMarketRepository,
)
from polywatch.infrastructure.db.repositories.trade_repository import (
    SqlAlchemyTradeRepository,
)
from polywatch.infrastructure.db.repositories.user_repository import (
    SqlAlchemyUserRepository,
)
from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
    SqlAlchemyWalletStatsRepository,
)
from polywatch.infrastructure.db.repositories.watchlist_repository import (
    SqlAlchemyWatchlistRepository,
)
from polywatch.infrastructure.polymarket.order_filled_decoder import (
    decode_order_filled,
    order_filled_logs,
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; e2e test DB di-skip",
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "t005c_receipt_orderfilled.json"
_RECEIPT: dict[str, object] = json.loads(_FIXTURE.read_text())
_FILLS = [decode_order_filled(log) for log in order_filled_logs(_RECEIPT)]
_TX1 = str(_RECEIPT["tx_hash"])
#: Salinan receipt dengan tx_hash berbeda (wallet sama trade lagi → cooldown).
_TX2 = "0x" + "ab" * 32
_RECEIPT2: dict[str, object] = {**_RECEIPT, "tx_hash": _TX2}

EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
WALLET_MAKER = "0xd6d2b81f0c60e358500a84ce8fbd566bbcc73f40"
WALLET_REAL = "0x59178ed0f66bcfa02193bcc77097715399bedb33"
TOKEN_ID = 78130277020315265342568704131302663094300398386838674211337072607396888275782

OWNER_TELEGRAM_ID = 988_001_001  # unik untuk e2e agar tidak bentrok user lain

# Waktu market: 5m window yang sedang LIVE saat T0 (start tepat 11:55,
# kelipatan 5m — identitas jendela discovery slug, T-024).
T0 = datetime(2026, 11, 8, 12, 2, 0, tzinfo=UTC)
SLUG_LIVE = f"btc-updown-5m-{int((T0 - timedelta(minutes=2)).timestamp())}"
SLUG_UPCOMING = f"eth-updown-5m-{int((T0 + timedelta(minutes=8)).timestamp())}"
TOKEN_UP = str(TOKEN_ID)
TOKEN_DOWN = str(TOKEN_ID + 1)


class StubClock:
    """Jam deterministik: maju tiap pemanggilan, bisa dimundurkan di test."""

    def __init__(self, start: datetime = T0, step_s: float = 1.0) -> None:
        self._now = start
        self._step = timedelta(seconds=step_s)

    def now(self) -> datetime:
        value = self._now
        self._now = self._now + self._step
        return value

    def set(self, moment: datetime) -> None:
        self._now = moment


def _receipt_fills(receipt: dict[str, object], tx_hash: str) -> list[FilledOrderSnapshot]:
    return [
        FilledOrderSnapshot(
            tx_hash=tx_hash,
            log_index=f.log_index,
            maker=f.maker,
            taker=f.taker,
            side=f.side,
            token_id=f.token_id,
            size=f.size,
            usdc=f.usdc,
            fee=f.fee,
        )
        for f in _FILLS
    ]


class FakeMarketSource:
    """MarketSource palsu: dua market + resolusi (resolved atau belum)."""

    def __init__(self, resolved: bool = True) -> None:
        self.resolved = resolved
        self.get_result_calls: list[str] = []

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        if slug == SLUG_LIVE:
            return MarketSnapshot(
                slug=SLUG_LIVE,
                condition_id="0xcond-live",
                token_up=TOKEN_UP,
                token_down=TOKEN_DOWN,
                volume_usdc=Decimal("125_000"),
                closed=False,
            )
        if slug == SLUG_UPCOMING:
            return MarketSnapshot(
                slug=SLUG_UPCOMING,
                condition_id="0xcond-upcoming",
                token_up="111111111111",
                token_down="222222222222",
                volume_usdc=Decimal("0"),
                closed=False,
            )
        return None

    async def list_markets(self, timeframe: Timeframe) -> list[MarketSnapshot]:
        return []

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        self.get_result_calls.append(slug)
        if slug != SLUG_LIVE:
            return None
        return MarketResultSnapshot(
            slug=slug,
            open_price=Decimal("1_950.00") if self.resolved else None,
            close_price=Decimal("1_952.00") if self.resolved else None,
            volume_usdc=Decimal("125_000"),
            resolved=self.resolved,
            outcome=Outcome.DOWN if self.resolved else None,
        )


class FakePriceSource:
    """PriceSource palsu — selalu tersedia (utama)."""

    name = "fake-1m"

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        return Decimal("1_950.00"), Decimal("1_951.50")


class FakeNotifier:
    """Notifier palsu yang mencatat semua pesan (tanpa jaringan)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str, chat_id: int | None = None) -> None:
        self.sent.append(msg)


class FakeStream:
    """TradeEventStream palsu: event WS = tx_hash receipt fixture."""

    def __init__(self, events: list[TradeEventSnapshot]) -> None:
        self._events = events

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        for event in self._events:
            yield event


class FakeReceipts:
    """ReceiptFetcher palsu: decode receipt fixture nyata dari docs/spikes/."""

    def __init__(self) -> None:
        self._fills: dict[str, list[FilledOrderSnapshot]] = {
            _TX1: _receipt_fills(_RECEIPT, _TX1),
            _TX2: _receipt_fills(_RECEIPT2, _TX2),
        }

    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot] | None:
        return self._fills.get(tx_hash)


_TRUNCATE_SQL = text(
    "TRUNCATE TABLE alerts_sent, alert_settings, watchlist, wallet_stats, "
    "positions, trades, market_results, markets, watch_config, assets, users "
    "RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Session DB nyata + truncate tabel yang dipakai (sebelum & sesudah)."""
    engine = create_async_engine(TEST_DSN or "")
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)
    await engine.dispose()


async def _market_id_by_slug(session: AsyncSession, slug: str) -> int:
    return int(await session.scalar(select(MarketModel.id).where(MarketModel.slug == slug)) or 0)


async def _trades_by_tx(session: AsyncSession, tx_hash: str) -> list[Trade]:
    rows = (
        await session.scalars(
            select(TradeModel).where(TradeModel.tx_hash == tx_hash).order_by(TradeModel.id)
        )
    ).all()
    return [
        Trade(
            tx_hash=r.tx_hash,
            log_index=r.log_index,
            market_id=r.market_id,
            wallet=r.wallet,
            outcome=r.outcome,
            action=r.action,
            price=r.price,
            size=r.size,
            usdc=r.usdc,
            traded_at=r.traded_at,
        )
        for r in rows
    ]


async def test_e2e_core_flow(session: AsyncSession) -> None:
    """Alur penuh: discover → trade → alert → resolve → skor (DB nyata)."""
    clock = StubClock(start=T0, step_s=2.0)
    notifier = FakeNotifier()
    market_source = FakeMarketSource()
    markets = SqlAlchemyMarketRepository(session)
    trades = SqlAlchemyTradeRepository(session)
    users = SqlAlchemyUserRepository(session)
    wallet_stats = SqlAlchemyWalletStatsRepository(session)
    watchlist = SqlAlchemyWatchlistRepository(session)
    alert_settings = SqlAlchemyAlertSettingsRepository(session)
    alerts_sent = SqlAlchemyAlertsSentRepository(session)

    # --- 1. discover_markets: market live + upcoming tersimpan, aset auto ---
    async def _no_sleep(_: float) -> None:
        return None

    discover = DiscoverMarkets(
        source=cast(MarketSource, market_source),
        repository=cast(MarketRepository, markets),
        clock=StubClock(start=T0, step_s=0),
        default_assets=("btc", "eth"),
        sleep=_no_sleep,
    )
    summary = await discover.execute()
    await session.commit()
    assert summary.new_markets == 2
    live_market = await markets.get_by_slug(SLUG_LIVE)
    assert live_market is not None
    assert live_market.status is MarketStatus.LIVE
    upcoming_market = await markets.get_by_slug(SLUG_UPCOMING)
    assert upcoming_market is not None
    assert upcoming_market.status is MarketStatus.UPCOMING
    live_market_id = await _market_id_by_slug(session, SLUG_LIVE)
    upcoming_market_id = await _market_id_by_slug(session, SLUG_UPCOMING)
    assert live_market_id > 0
    assert upcoming_market_id > 0
    symbols = set((await session.scalars(select(AssetModel.symbol))).all())
    assert {"btc", "eth"} <= symbols  # aset ter-auto-detect dari slug

    # --- 2. ingest_trades: trade tersimpan dari receipt fixture nyata ---
    # wallet_stats sudah ada dari market sebelumnya → trade baru memicu alert.
    await wallet_stats.upsert(
        WalletStats(
            wallet=WALLET_REAL,
            markets_count=60,
            wins=40,
            avg_entry=Decimal("0.55"),
            pnl=Decimal("120"),
            staked=Decimal("3_300"),
            is_market_maker=False,
            score=Decimal("80"),
            last_trade_at=T0,
        )
    )
    user_id = await users.get_or_create_by_telegram(OWNER_TELEGRAM_ID)
    await alert_settings.save(
        user_id,
        {
            "min_score": Decimal("70"),
            # fill fixture bernilai 1 USDC → ambang minimal 0 agar alert terpicu
            "min_usdc": Decimal("0"),
            "cooldown_sec": 300,
            "enabled": True,
        },
    )
    await session.commit()

    evaluator = EvaluateAlert(
        wallet_stats=wallet_stats,
        markets=cast(MarketRepository, markets),
        users=users,
        watchlist=cast("WatchlistRepository", watchlist),
        settings_repo=cast("AlertSettingsRepository", alert_settings),
        alerts_sent=cast("AlertsSentRepository", alerts_sent),
        notifier=cast(Notifier, notifier),
        clock=clock,
        telegram_id=OWNER_TELEGRAM_ID,
    )

    async def on_saved(new_trades: Sequence[Trade]) -> None:
        for trade in new_trades:
            await evaluator.evaluate(trade)
        await session.commit()

    market_info = TokenMarketInfo(
        market_id=live_market_id,
        condition_id="0xcond-live",
        token_up=TOKEN_UP,
        token_down=TOKEN_DOWN,
        slug=SLUG_LIVE,
    )
    ingest = IngestTrades(
        stream=FakeStream(
            [
                TradeEventSnapshot(
                    tx_hash=_TX1, asset_id=TOKEN_UP, condition_id="0xcond-live", timestamp_ms=1
                ),
                # trade kedua wallet sama (tx berbeda) → di dalam cooldown.
                TradeEventSnapshot(
                    tx_hash=_TX2, asset_id=TOKEN_UP, condition_id="0xcond-live", timestamp_ms=2
                ),
            ]
        ),
        receipts=cast(ReceiptFetcher, FakeReceipts()),
        trades=cast(TradeRepository, trades),
        markets=cast(MarketRepository, markets),
        token_markets=[market_info],
        clock=clock,
        on_saved=on_saved,
    )
    stats = await ingest.run_once()
    await session.commit()
    assert stats.events_seen == 2
    assert (
        stats.trades_saved == 6
    )  # 3 baris per tx (fill#1 maker+taker wallet, fill#2 maker), dua tx
    saved1 = await _trades_by_tx(session, _TX1)
    saved2 = await _trades_by_tx(session, _TX2)
    # fill#1 (SELL): maker WALLET_MAKER + taker WALLET_REAL keduanya sell;
    # fill#2 (BUY): maker WALLET_REAL buy, taker = exchange → tidak jadi trade.
    assert {(t.wallet, t.action.value) for t in saved1} == {
        (WALLET_MAKER, "sell"),
        (WALLET_REAL, "sell"),
        (WALLET_REAL, "buy"),
    }

    # --- 3. hook on_saved → evaluate_alert: tepat 1 pesan (cooldown) ---
    assert len(notifier.sent) == 1
    assert "Alert copy-trade" in notifier.sent[0]
    assert WALLET_REAL[:6] in notifier.sent[0]
    rows = (await session.scalars(select(AlertSentModel))).all()
    assert len(rows) == 1
    assert rows[0].wallet == WALLET_REAL
    assert 0 <= rows[0].delay_ms < 60_000  # delay_ms wajar

    # --- 4. record_result: market_results + status jadi resolved ---
    await markets.update_status(live_market_id, MarketStatus.CLOSED)
    await session.commit()
    record_result = RecordResult(
        source=cast(MarketSource, market_source),
        repository=cast(MarketRepository, markets),
        primary_price=cast(PriceSource, FakePriceSource()),
        clock=clock,
    )
    results = await record_result.execute()
    await session.commit()
    assert [r.status for r in results if isinstance(r, RecordResultSummary)] == ["resolved"]
    saved_result = await markets.get_result(live_market_id)
    assert saved_result is not None
    assert saved_result.result is Outcome.DOWN  # dari resolusi resmi (fake Gamma)
    assert saved_result.price_source == "fake-1m"
    assert saved_result.open_price == Decimal("1_950.00")
    assert saved_result.close_price == Decimal("1_951.50")
    after = await markets.get_by_slug(SLUG_LIVE)
    assert after is not None
    assert after.status is MarketStatus.RESOLVED

    # --- 5. recompute_scores: wallet_stats benar + idempoten ---
    policy = ScoringPolicy(min_sample=1)  # e2e hanya punya 1 market
    recompute = RecomputeScores(
        trades=cast(TradeRepository, trades),
        wallet_stats=wallet_stats,
        policy=policy,
    )
    score_summary = await recompute.execute(live_market_id)
    await session.commit()
    assert score_summary.wallets_processed == 2

    # Nilai diharapkan dihitung ulang lewat domain murni dari trade tersimpan.
    expected = {
        wallet: _expected_stats(session_tx=[*saved1, *saved2], wallet=wallet, policy=policy)
        for wallet in (WALLET_REAL, WALLET_MAKER)
    }
    stats_real = await wallet_stats.get(WALLET_REAL)
    assert stats_real is not None
    stats_maker = await wallet_stats.get(WALLET_MAKER)
    assert stats_maker is not None
    assert stats_real.markets_count == 1
    assert stats_real.pnl == expected[WALLET_REAL].pnl
    # staked tidak disimpan di tabel wallet_stats (lihat repo _to_domain).
    assert stats_real.score == _round6(expected[WALLET_REAL].score)
    assert stats_real.edge == _round6(expected[WALLET_REAL].edge)
    assert stats_real.roi == _round6(expected[WALLET_REAL].roi)
    assert stats_maker.pnl == expected[WALLET_MAKER].pnl
    assert stats_maker.score == _round6(expected[WALLET_MAKER].score)
    # Sanity: WALLET_REAL membeli dan menjual token netral → pnl 0 (posisi net nol).
    assert stats_real.pnl == Decimal("0")
    assert stats_real.avg_entry is not None
    assert stats_real.avg_entry == Decimal("0.1200")  # 3 buy total USDC 2 / 16.666668
    # Wallet pemegang sisi kalah murni: buat trade WALLET_MAKER buy saja.
    maker_own = [t for t in saved1 if t.wallet == WALLET_MAKER]
    maker_agg = aggregate_market(maker_own[0].market_id, maker_own, winning=Outcome.DOWN)
    assert maker_agg.pnl > 0  # jual Up saat biaya bersih negatif → pnl positif

    # Idempoten: recompute ulang → nilai sama.
    await recompute.execute(live_market_id)
    await session.commit()
    stats_real2 = await wallet_stats.get(WALLET_REAL)
    assert stats_real2 is not None
    assert stats_real2.pnl == stats_real.pnl
    assert stats_real2.score == stats_real.score

    # --- 6a. wallet market maker tidak memicu alert ---
    await wallet_stats.upsert(
        WalletStats(
            wallet=WALLET_MAKER,
            markets_count=60,
            wins=30,
            avg_entry=Decimal("0.50"),
            pnl=Decimal("10"),
            staked=Decimal("3_000"),
            is_market_maker=True,  # market maker → tidak alertable
            score=Decimal("75"),
            last_trade_at=T0,
        )
    )
    await session.commit()
    trade_maker = next(t for t in saved1 if t.wallet == WALLET_MAKER)
    clock.set(T0 + timedelta(seconds=10))
    decision_maker = await evaluator.evaluate(trade_maker)
    await session.commit()
    assert decision_maker.sent is False
    assert decision_maker.reason == "not_alertable"
    assert len(notifier.sent) == 1  # tetap cuma 1 pesan

    # --- 6b. market belum resolved → tidak menghasilkan result palsu ---
    await markets.update_status(upcoming_market_id, MarketStatus.CLOSED)
    await session.commit()
    unresolved_source = FakeMarketSource(resolved=False)
    record_unresolved = RecordResult(
        source=cast(MarketSource, unresolved_source),
        repository=cast(MarketRepository, markets),
        primary_price=cast(PriceSource, FakePriceSource()),
        clock=clock,
        poll_interval_sec=0.0,
        timeout_sec=0.0,  # langsung timeout: resolusi resmi belum keluar
    )
    summaries = await record_unresolved.execute()
    await session.commit()
    statuses = [r.status for r in summaries if isinstance(r, RecordResultSummary)]
    assert "resolved" not in statuses  # tidak ada result palsu
    assert await markets.get_result(upcoming_market_id) is None
    # Market kedua (upcoming → closed) diproses tapi resolusinya belum keluar.
    assert SLUG_UPCOMING in unresolved_source.get_result_calls


def _expected_stats(session_tx: list[Trade], wallet: str, policy: ScoringPolicy) -> WalletStats:
    """Hitung nilai diharapkan lewat domain murni (tanpa DB)."""
    own = [t for t in session_tx if t.wallet == wallet]
    aggregate = aggregate_market(own[0].market_id, own, winning=Outcome.DOWN)
    return build_wallet_stats(wallet, [aggregate], policy)


def _round6(value: Decimal) -> Decimal:
    """Bulatkan ke 6 desimal — skala NUMERIC kolom wallet_stats."""
    return value.quantize(Decimal("0.000001"))
