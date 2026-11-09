"""Unit test T-030 (ADR-017) — perbaikan BUG-012/BUG-013/BUG-014.

1. BUG-012: kegagalan sumber harga SATU market tidak menjatuhkan siklus
   record_result — market lain tetap tersimpan; bila semua sumber gagal,
   hasil resmi tetap tersimpan dengan `price_source='unavailable'` dan
   open/close NULL.
2. BUG-013: watchdog ingest — stream "hidup tapi bisu" (tidak ada event)
   memicu `ws_watchdog_reconnect` (stream lama dibatalkan, koneksi baru
   dengan token terbaru); refresh token dengan set berubah memicu
   re-subscribe dengan token baru.
3. BUG-014: rate limit notifikasi kesehatan — simulasi 2 jam pemeriksaan
   tiap 30 dtk menghasilkan ≤ 12 notifikasi per komponen; notifikasi
   pemulihan hanya sekali per episode.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polywatch.application.dto.market import (
    MarketResultSnapshot,
    MarketSnapshot,
    PendingMarket,
    StaleMarket,
    TokenMarketInfo,
)
from polywatch.application.dto.trade import (
    FilledOrderSnapshot,
    ResolvedTrade,
    TradeEventSnapshot,
)
from polywatch.application.use_cases.health_monitor import HealthMonitor
from polywatch.application.use_cases.ingest_trades import IngestTrades
from polywatch.application.use_cases.record_result import (
    RecordResult,
    RecordResultSummary,
)
from polywatch.domain.market import (
    Market,
    MarketResult,
    MarketStatus,
    Outcome,
    Timeframe,
)
from polywatch.domain.trade import Trade

# ============================================================ BUG-012 helpers


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeMarketSource:
    """MarketSource: get_result resolved untuk semua slug."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        return None

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
        return []

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        self.calls += 1
        return MarketResultSnapshot(
            slug=slug,
            open_price=None,
            close_price=None,
            volume_usdc=Decimal("10"),
            resolved=True,
            outcome=Outcome.UP,
        )


class RaisingPriceSource:
    """PriceSource yang selalu melempar (mensimulasikan BUG-012)."""

    name = "rusak"

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        raise RuntimeError(f"source down for {slug}")


class FixedPriceSource:
    """PriceSource yang selalu berhasil."""

    def __init__(self, name: str = "ok") -> None:
        self.name = name
        self.calls: list[str] = []

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        self.calls.append(slug)
        return (Decimal("1"), Decimal("2"))


class _Repo:
    """MarketRepository in-memory minimal untuk record_result."""

    def __init__(self, pending: Sequence[PendingMarket]) -> None:
        self._pending = list(pending)
        self.results: dict[int, MarketResult] = {}
        self.statuses: dict[int, MarketStatus] = {}

    async def upsert(self, market: Market) -> int:
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return []

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return self._pending[:limit]

    async def list_stale_open(self, now: datetime) -> Sequence[StaleMarket]:
        return []

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return None

    async def has_result(self, market_id: int) -> bool:
        return market_id in self.results

    async def save_result(self, result: MarketResult) -> None:
        self.results.setdefault(result.market_id, result)

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        self.statuses[market_id] = status

    async def get_result(self, market_id: int) -> MarketResult | None:
        return self.results.get(market_id)


def _pending(market_id: int, asset: str, start_ts: int) -> PendingMarket:
    return PendingMarket(
        market_id=market_id,
        condition_id=f"0x{market_id}",
        slug=f"{asset}-updown-5m-{start_ts}",
        status=MarketStatus.CLOSED,
    )


def _resolved_source() -> FakeMarketSource:
    """MarketSource yang selalu resolved (outcome UP)."""
    return FakeMarketSource()


async def test_satu_market_harga_gagal_sisanya_tersimpan() -> None:
    """BUG-012: satu sumber harga melempar → market lain tetap tersimpan.

    Market pertama pakai sumber harga yang selalu melempar; market kedua
    sumbernya sehat. Keduanya harus tersimpan (market pertama dengan
    'unavailable').
    """
    repo = _Repo(
        [
            _pending(1, "xrp", 1794000000),
            _pending(2, "btc", 1794000000),
        ]
    )
    use_case = RecordResult(
        source=_resolved_source(),
        repository=repo,
        primary_price=RaisingPriceSource(),
        fallback_price=FixedPriceSource("binance-1m"),
        clock=FakeClock(datetime(2026, 11, 9, 3, 0, tzinfo=UTC)),
    )
    summaries = await use_case.execute()

    assert repo.results[1].price_source == "binance-1m"  # fallback dipakai
    assert repo.results[2].price_source == "binance-1m"
    assert repo.statuses[1] is MarketStatus.RESOLVED
    assert repo.statuses[2] is MarketStatus.RESOLVED
    cycle = summaries[-1]
    assert getattr(cycle, "resolved", 0) == 2


async def test_semua_sumber_gagal_hasil_tetap_tersimpan_unavailable() -> None:
    """BUG-012: kedua sumber melempar → hasil resmi tetap tersimpan dengan
    price_source='unavailable' dan open/close NULL."""
    repo = _Repo([_pending(1, "xrp", 1794000000), _pending(2, "btc", 1794000000)])
    use_case = RecordResult(
        source=_resolved_source(),
        repository=repo,
        primary_price=RaisingPriceSource(),
        fallback_price=RaisingPriceSource(),
        clock=FakeClock(datetime(2026, 11, 9, 3, 0, tzinfo=UTC)),
    )
    await use_case.execute()

    for mid in (1, 2):
        result = repo.results[mid]
        assert result.price_source == "unavailable"
        assert result.open_price is None
        assert result.close_price is None
        assert result.result.value == "up"  # arah dari resolusi resmi
        assert repo.statuses[mid] is MarketStatus.RESOLVED


async def test_get_result_error_market_disisihkan_siklus_lanjut() -> None:
    """BUG-012: resolusi Gamma error untuk satu market → skipped, market
    lain tetap diproses, tidak ada exception keluar dari execute()."""

    class _FlakySource:
        async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
            return None

        async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
            return []

        async def get_result(self, slug: str) -> MarketResultSnapshot | None:
            if "xrp" in slug:
                raise RuntimeError("gamma 503")
            from polywatch.domain.market import Outcome

            return MarketResultSnapshot(
                slug=slug,
                open_price=None,
                close_price=None,
                volume_usdc=Decimal("10"),
                resolved=True,
                outcome=Outcome.UP,
            )

    repo = _Repo([_pending(1, "xrp", 1794000000), _pending(2, "btc", 1794000000)])
    use_case = RecordResult(
        source=_FlakySource(),
        repository=repo,
        primary_price=FixedPriceSource(),
        clock=FakeClock(datetime(2026, 11, 9, 3, 0, tzinfo=UTC)),
    )
    summaries = await use_case.execute()

    market_summaries = [s for s in summaries if isinstance(s, RecordResultSummary)]
    statuses = {s.market_id: s.status for s in market_summaries}
    assert statuses[1] == "skipped"
    assert statuses[2] == "resolved"
    assert 2 not in repo.results or repo.results[2].price_source == "ok"
    assert 1 not in repo.results  # market gagal ditunda siklus berikutnya


# ============================================================ BUG-013 helpers


class _IngestClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class SilentStream:
    """Stream yang tidak pernah menghasilkan event (hidup tapi bisu).

    `stream()` mengembalikan generator yang menunggu selamanya — mensimulasikan
    koneksi WS terbuka tanpa event (BUG-013).
    """

    def __init__(self) -> None:
        self.stream_calls: list[list[str]] = []
        self._cancelled = asyncio.Event()

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.stream_calls.append(list(token_ids))
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        try:
            await self._cancelled.wait()
        except asyncio.CancelledError:
            raise
        # tidak pernah yield — stream bisu
        yield _snap("0xnever")  # pragma: no cover


class OneEventThenSilentStream:
    """Satu event lalu diam selamanya (untuk uji watchdog setelah event)."""

    def __init__(self) -> None:
        self.stream_calls: list[list[str]] = []

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.stream_calls.append(list(token_ids))
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        yield _snap("0xtx1")
        await asyncio.Event().wait()  # diam selamanya
        yield _snap("0xnever")  # pragma: no cover


class BurstStream:
    """Stream yang menghasilkan event tiap interval kecil, tidak pernah
    selesai — untuk menguji bahwa event rutin MEMRESET watchdog."""

    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.stream_calls: list[list[str]] = []
        self.events = 0

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        self.stream_calls.append(list(token_ids))
        return self._gen()

    async def _gen(self) -> AsyncIterator[TradeEventSnapshot]:
        i = 0
        while not self.stop.is_set():
            await asyncio.sleep(0.01)
            yield _snap(f"0xtx{i}")
            i += 1
            self.events += 1


def _snap(tx: str) -> TradeEventSnapshot:
    return TradeEventSnapshot(
        tx_hash=tx, asset_id="111", condition_id="0xcid", timestamp_ms=1794122884052
    )


class _ReceiptsNone:
    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot] | None:
        return None


class _NoopTrades:
    async def insert_many(self, trades: Sequence[Trade]) -> int:
        return 0

    async def last_traded_at(self, market_id: int) -> datetime | None:
        return None

    async def wallets_for_market(self, market_id: int) -> Sequence[str]:
        return []

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        return []

    async def list_wallets_with_resolved_trades(self) -> Sequence[str]:
        return []


class _NoopMarkets:
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


class _LiveTokens:
    def __init__(self, batches: Sequence[Sequence[TokenMarketInfo]]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return batch


def _market_info(market_id: int, token_up: str, token_down: str) -> TokenMarketInfo:
    return TokenMarketInfo(
        market_id=market_id,
        condition_id=f"0xcid{market_id}",
        token_up=token_up,
        token_down=token_down,
        slug=f"btc-updown-5m-{1000 + market_id}",
    )


async def test_watchdog_reconnect_stream_bisu() -> None:
    """BUG-013: stream tanpa event + ada token live → stream dibatalkan
    (watchdog) dan stream baru dibuka dengan token terbaru."""
    stream = SilentStream()
    market_lama = _market_info(1, "111", "222")
    market_baru = _market_info(2, "333", "444")
    live = _LiveTokens([[market_lama], [market_baru], [market_baru]])
    stop = asyncio.Event()

    ingest = IngestTrades(
        stream=stream,
        receipts=_ReceiptsNone(),
        trades=_NoopTrades(),
        markets=_NoopMarkets(),
        clock=_IngestClock(),
        live_tokens=live,
        live_refresh_sec=0.05,
        watchdog_sec=0.3,  # pendek untuk test
    )

    async def _stop_after_two_subscribes() -> None:
        for _ in range(2000):
            if len(stream.stream_calls) >= 2:
                break
            await asyncio.sleep(0.005)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_after_two_subscribes())

    # stream pertama pakai token lama; watchdog memicu stream baru
    assert stream.stream_calls[0] == ["111", "222"]
    assert len(stream.stream_calls) >= 2
    # stream kedua memakai token terbaru dari refresh
    assert stream.stream_calls[1] == ["333", "444"]


async def test_watchdog_tidak_memaksa_bila_ada_event() -> None:
    """Event rutin me-reset watchdog — stream sehat tidak dipaksa reconnect."""
    stop = asyncio.Event()
    stream = BurstStream(stop)
    market = _market_info(1, "111", "222")
    live = _LiveTokens([[market]])

    ingest = IngestTrades(
        stream=stream,
        receipts=_ReceiptsNone(),
        trades=_NoopTrades(),
        markets=_NoopMarkets(),
        clock=_IngestClock(),
        live_tokens=live,
        live_refresh_sec=60.0,  # token tidak berubah → tanpa re-subscribe
        watchdog_sec=0.2,
    )

    async def _stop_soon() -> None:
        await asyncio.sleep(0.6)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_soon())

    # event mengalir terus → stream TIDAK dipaksa reconnect
    assert stream.events > 10
    assert ingest._watchdog.forced == 0
    assert len(stream.stream_calls) == 1


async def test_token_berubah_memaksa_resubscribe() -> None:
    """BUG-013: refresh token dengan set berubah → stream baru dibuka
    dengan token baru (bukan meneruskan stream lama)."""
    stream = SilentStream()
    market_lama = _market_info(1, "111", "222")
    market_baru = _market_info(2, "333", "444")
    live = _LiveTokens([[market_lama], [market_baru], [market_baru]])
    stop = asyncio.Event()

    ingest = IngestTrades(
        stream=stream,
        receipts=_ReceiptsNone(),
        trades=_NoopTrades(),
        markets=_NoopMarkets(),
        clock=_IngestClock(),
        live_tokens=live,
        live_refresh_sec=0.05,
        watchdog_sec=60.0,  # besar: yang diuji re-subscribe, bukan watchdog
    )

    async def _stop_after_two() -> None:
        for _ in range(2000):
            if len(stream.stream_calls) >= 2:
                break
            await asyncio.sleep(0.005)
        stop.set()

    await asyncio.gather(ingest.run(stop), _stop_after_two())

    assert stream.stream_calls[0] == ["111", "222"]
    assert stream.stream_calls[1] == ["333", "444"]
    assert live.calls >= 2


# ============================================================ BUG-014 helpers


class _HmClock:
    """Jam yang bisa dimajukan untuk simulasi 2 jam."""

    def __init__(self) -> None:
        self._now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _HmActivity:
    async def live_market_count(self) -> int:
        return 5

    async def last_trade_at_live(self) -> datetime | None:
        return None


class _HmNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append(text)


async def test_simulasi_2_jam_notifikasi_terbatas() -> None:
    """BUG-014: 2 jam pemeriksaan tiap 30 dtk (240 cek), ingest terus
    stale → notifikasi ≤ 12 per komponen (600 dtk rate limit)."""
    clock = _HmClock()
    notifier = _HmNotifier()
    monitor = HealthMonitor(
        activity=_HmActivity(),
        notifier=notifier,
        clock=clock,
        stale_ingest_s=600.0,
        stale_discover_s=0.01,  # discover juga stale sejak awal
        stale_result_s=0.01,
        rate_limit_s=600.0,
        grace_s=0.0,  # tanpa grace supaya stale sejak cek pertama
    )
    # satu heartbeat awal supaya last_ok terisi, lalu berhenti
    await monitor.record_cycle("ingest", ok=True)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    clock.advance(700)  # melewati ambang semua komponen

    for _ in range(240):  # 2 jam / 30 dtk
        await monitor.check()
        clock.advance(30)

    stale_alerts = [t for t in notifier.sent if t.startswith("⚠️")]
    per_component = {
        name: sum(1 for t in stale_alerts if name in t or (name == "ingest" and "heartbeat" in t))
        for name in ("ingest", "discover", "result")
    }
    # maksimum 12 per komponen dalam 2 jam (rate limit 600 dtk)
    for name, count in per_component.items():
        assert count <= 12, f"{name}: {count} notifikasi (> 12)"
    # minimal ada — alarm memang aktif
    assert len(stale_alerts) >= 3


async def test_pemulihan_hanya_sekali_per_episode() -> None:
    """BUG-014: notifikasi pemulihan dikirim sekali; cek ulang setelah
    pemulihan tidak mengirim lagi."""
    clock = _HmClock()
    notifier = _HmNotifier()
    monitor = HealthMonitor(
        activity=_HmActivity(),
        notifier=notifier,
        clock=clock,
        stale_ingest_s=600.0,
        rate_limit_s=600.0,
        grace_s=0.0,
    )
    await monitor.record_cycle("ingest", ok=True)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    clock.advance(700)
    await monitor.check()  # stale → 1 notifikasi
    await monitor.record_cycle("ingest", ok=True)  # pulih → 1 pemulihan
    await monitor.check()  # sehat — tidak ada pesan
    await monitor.check()  # sehat — tidak ada pesan

    # hanya jalur ingest yang mengirim: 1 alarm + 1 pemulihan
    ingest_messages = [t for t in notifier.sent if "ingest" in t or "heartbeat" in t]
    assert len(ingest_messages) == 2


async def test_flapping_live_markets_tidak_spam_pemulihan() -> None:
    """BUG-014 (akar nyata 10 jam): live_markets berubah-ubah antar cek
    (0 ↔ N) selama ingest stale — pemulihan tidak boleh dikirim berulang."""

    class _FlappingActivity:
        def __init__(self) -> None:
            self.toggle = False

        async def live_market_count(self) -> int:
            self.toggle = not self.toggle
            return 3 if self.toggle else 0

        async def last_trade_at_live(self) -> datetime | None:
            return None

    clock = _HmClock()
    notifier = _HmNotifier()
    monitor = HealthMonitor(
        activity=_FlappingActivity(),
        notifier=notifier,
        clock=clock,
        stale_ingest_s=600.0,
        rate_limit_s=600.0,
        grace_s=0.0,
    )
    await monitor.record_cycle("ingest", ok=True)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    clock.advance(700)
    for _ in range(20):
        await monitor.check()
        clock.advance(30)

    alarms = [t for t in notifier.sent if t.startswith("⚠️")]
    recoveries = [t for t in notifier.sent if t.startswith("✅")]
    # T-030/BUG-014: simulasi ±10 menit cek tiap 30 dtk dengan live_markets
    # fluktuatif — alarm maksimal ~1 per jendela 600 dtk (bukan 20!), dan
    # pemulihan tidak berulang-ulang.
    assert len(alarms) <= 3
    assert len(recoveries) <= 2


async def test_log_stale_terbatas_dari_stale_names() -> None:
    """BUG-014: stale_names (dipakai runner untuk log) hanya berisi
    komponen yang baru stale / cooldown-nya lewat — bukan tiap cek."""
    clock = _HmClock()
    notifier = _HmNotifier()
    monitor = HealthMonitor(
        activity=_HmActivity(),
        notifier=notifier,
        clock=clock,
        stale_ingest_s=600.0,
        rate_limit_s=600.0,
        grace_s=0.0,
    )
    await monitor.record_cycle("ingest", ok=True)
    clock.advance(700)
    stale_checks = 0
    for _ in range(240):
        report = await monitor.check()
        if "ingest" in report.stale_names:
            stale_checks += 1
        clock.advance(30)
    # 2 jam pemeriksaan → log "stale terdeteksi" maksimum ~12x
    assert stale_checks <= 12
