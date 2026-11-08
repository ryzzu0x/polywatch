"""Unit test T-029: siklus cepat, berbatas, ambang adaptif, cache negatif.

Empat hal yang dibuktikan (bukti diminta TASKS T-029):

1. **Semaphore** membatasi konkurensi fetch Gamma per siklus discover.
2. **Batch size** membatasi jumlah market CLOSED per siklus record_result
   dan jumlah kandidat sweep.
3. **Ambang adaptif** health monitor bereaksi pada durasi siklus terukur
   (`record_cycle(kind, ok, duration_s)`).
4. **Cache negatif** aset mati — aset tanpa market tidak di-query ulang
   sampai TTL habis.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from polywatch.application.dto.market import (
    MarketResultSnapshot,
    MarketSnapshot,
    PendingMarket,
    StaleMarket,
    TokenMarketInfo,
)
from polywatch.application.use_cases.discover_markets import DiscoverMarkets
from polywatch.application.use_cases.health_monitor import (
    ADAPTIVE_STALE_MULTIPLIER,
    DEFAULT_STALE_DISCOVER_SEC,
    HealthMonitor,
)
from polywatch.application.use_cases.record_result import (
    DEFAULT_BATCH_SIZE,
    RecordResult,
    RecordResultCycleSummary,
    RecordResultSummary,
)
from polywatch.application.use_cases.sweep_market_status import (
    DEFAULT_SWEEP_BATCH,
    SweepMarketStatus,
)
from polywatch.domain.market import (
    Market,
    MarketResult,
    MarketSlug,
    MarketStatus,
    Outcome,
    Timeframe,
)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _SleepCounter:
    """Sleep palsu yang menghitung panggilan (tanpa menunggu)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _: float) -> None:
        self.calls += 1


async def _no_sleep(_: float) -> None:
    return None


class _ConcurrencyProbeSource:
    """SlugSource probe: catat konkurensi aktif maksimum saat fetch berjalan."""

    def __init__(self, delay_s: float = 0.02) -> None:
        self._delay_s = delay_s
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        self.calls.append(slug)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self._delay_s)
        finally:
            self.active -= 1
        return None


class _StaticAssetSource:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def list_symbols(self) -> list[str]:
        return self._symbols


class _FakeRepo:
    """MarketRepository in-memory minimal untuk discover/record/sweep."""

    def __init__(self) -> None:
        self.by_condition: dict[str, Market] = {}
        self.results: dict[int, object] = {}
        self.statuses: dict[int, MarketStatus] = {}
        self.pending: list[PendingMarket] = []
        self.stale_open: list[StaleMarket] = []
        self.pending_limit_seen: list[int] = []

    async def upsert(self, market: Market) -> int:
        if market.condition_id not in self.by_condition:
            self.by_condition[market.condition_id] = market
            return len(self.by_condition)
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        for m in self.by_condition.values():
            if m.slug == MarketSlug.parse(slug):
                return m
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return [m for m in self.by_condition.values() if m.status is status]

    async def get_result(self, market_id: int) -> MarketResult | None:
        return self.results.get(market_id)  # type: ignore[return-value]

    async def save_result(self, result: object) -> None:
        market_result = cast(MarketResult, result)
        self.results[market_result.market_id] = result

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        self.statuses[market_id] = status

    async def has_result(self, market_id: int) -> bool:
        return market_id in self.results

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        self.pending_limit_seen.append(limit)
        remaining = [p for p in self.pending if p.market_id not in self.results]
        return remaining[:limit]

    async def list_stale_open(self, now: datetime) -> Sequence[StaleMarket]:
        return self.stale_open

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return None


def _snapshot(slug: str, condition_id: str) -> MarketSnapshot:
    return MarketSnapshot(
        slug=slug,
        condition_id=condition_id,
        token_up="111",
        token_down="222",
        volume_usdc=Decimal("10"),
        closed=False,
    )


NOW = datetime(2026, 11, 8, 12, 3, 27, tzinfo=UTC)


def _make_discover(
    source: Any,
    repo: _FakeRepo,
    *,
    assets: tuple[str, ...] = ("btc",),
    max_concurrency: int = 5,
    monotonic: Any = None,
    dead_asset_ttl_s: float = 600.0,
) -> DiscoverMarkets:
    return DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=assets,
        max_concurrency=max_concurrency,
        dead_asset_ttl_s=dead_asset_ttl_s,
        monotonic=monotonic if monotonic is not None else _FakeMonotonic(),
        fetch_delay_s=0.0,
        sleep=_no_sleep,
    )


class _FakeMonotonic:
    """Jam monoton palsu yang bisa dimajukan untuk test TTL cache negatif."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ------------------------------------------------------------------ semaphore


async def test_semaphore_membatasi_konkurensi_fetch() -> None:
    """Dengan 8 aset x 2 tf x 6 jendela = 96 fetch, konkurensi maksimum = 5."""
    repo = _FakeRepo()
    source = _ConcurrencyProbeSource()
    discover = _make_discover(source, repo, assets=tuple(f"a{i}" for i in range(8)))

    await discover.execute()

    total_windows = 8 * 2 * (3 + 2 + 1)  # past:3 + current + ahead:2
    assert len(source.calls) == total_windows
    assert source.max_active == 5  # semaphore GAMMA_MAX_CONCURRENCY


async def test_semaphore_konkurensi_1_membuat_sekuensial() -> None:
    repo = _FakeRepo()
    source = _ConcurrencyProbeSource()
    discover = _make_discover(source, repo, assets=("btc",), max_concurrency=1)

    await discover.execute()

    assert source.max_active == 1


# ------------------------------------------------------------------ batch size


class _InstantResultSource:
    """MarketSource: semua get_result langsung resolved."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        return None

    async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
        return []

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        self.calls.append(slug)
        return MarketResultSnapshot(
            slug=slug,
            open_price=None,
            close_price=None,
            volume_usdc=Decimal("1"),
            resolved=True,
            outcome=Outcome.UP,
        )


class _InstantPrice:
    name = "fake"

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal]:
        return Decimal("1"), Decimal("2")


def _pending(market_id: int, slug: str) -> PendingMarket:
    return PendingMarket(
        market_id=market_id,
        condition_id=f"0x{market_id}",
        slug=slug,
        status=MarketStatus.CLOSED,
    )


def _make_record(repo: _FakeRepo, *, batch_size: int) -> RecordResult:
    return RecordResult(
        source=_InstantResultSource(),
        repository=repo,
        primary_price=_InstantPrice(),
        clock=FakeClock(NOW),
        batch_size=batch_size,
        sleep=_no_sleep,
    )


class _DelayedResultSource(_InstantResultSource):
    """Market 1 langsung resolved; market lain belum resolved pada poll ini."""

    def __init__(self) -> None:
        super().__init__()
        self.unresolved = {"btc-updown-5m-1794144000"}

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        if slug in self.unresolved:
            self.calls.append(slug)
            self.unresolved.discard(slug)  # resolved pada poll berikutnya
            return MarketResultSnapshot(
                slug=slug,
                open_price=None,
                close_price=None,
                volume_usdc=Decimal("1"),
                resolved=False,
                outcome=None,
            )
        return await super().get_result(slug)


async def test_satu_poll_per_siklus_belum_resolved_ditunda() -> None:
    """T-029: market belum resolved ditunda ke siklus berikutnya — tidak
    menunggu poll di dalam siklus (penyebab durasi result 128 dtk)."""
    repo = _FakeRepo()
    repo.pending = [
        _pending(1, "btc-updown-5m-1794144000"),
        _pending(2, "eth-updown-5m-1794144300"),
    ]
    source = _DelayedResultSource()
    record = RecordResult(
        source=source,
        repository=repo,
        primary_price=_InstantPrice(),
        clock=FakeClock(datetime(2026, 11, 8, 13, 20, tzinfo=UTC)),
        batch_size=10,
        sleep=_no_sleep,
    )

    first = await record.execute()
    first_market = [s for s in first if isinstance(s, RecordResultSummary)]
    cycle_first = next(s for s in first if isinstance(s, RecordResultCycleSummary))
    # btc belum resolved → ditunda (deferred), eth resolved
    assert [(s.market_id, s.status) for s in first_market] == [(2, "resolved")]
    assert cycle_first.resolved == 1

    second = await record.execute()  # siklus berikutnya: btc resolved
    second_market = [s for s in second if isinstance(s, RecordResultSummary)]
    assert [(s.market_id, s.status) for s in second_market] == [(1, "resolved")]
    assert source.calls.count("btc-updown-5m-1794144000") == 2  # satu poll per siklus


async def test_market_tua_belum_resolved_timeout_notifikasi() -> None:
    """Market melewati timeout_sec (dari end_at) → status timeout + notifikasi."""
    repo = _FakeRepo()
    repo.pending = [
        _pending(1, "btc-updown-5m-1794144000"),  # end 13:20, now 13:40
    ]

    class _NeverResolved(_InstantResultSource):
        async def get_result(self, slug: str) -> MarketResultSnapshot | None:
            self.calls.append(slug)
            return MarketResultSnapshot(
                slug=slug,
                open_price=None,
                close_price=None,
                volume_usdc=Decimal("1"),
                resolved=False,
                outcome=None,
            )

    notifier = _NoopNotifier()
    record = RecordResult(
        source=_NeverResolved(),
        repository=repo,
        primary_price=_InstantPrice(),
        notifier=notifier,
        owner_chat_id=555,
        clock=FakeClock(datetime(2026, 11, 8, 13, 40, tzinfo=UTC)),
        timeout_sec=600.0,
        batch_size=10,
        sleep=_no_sleep,
    )
    summaries = await record.execute()
    per_market = [s for s in summaries if isinstance(s, RecordResultSummary)]
    assert [(s.market_id, s.status) for s in per_market] == [(1, "timeout")]
    assert len(notifier.sent) == 1


async def test_batch_size_membatasi_market_per_siklus() -> None:
    """15 market pending, batch 10 → hanya 10 diproses; sisanya siklus berikut."""
    repo = _FakeRepo()
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    repo.pending = [
        _pending(
            i,
            f"btc-updown-5m-{int((start + timedelta(minutes=5 * i)).timestamp())}",
        )
        for i in range(15)
    ]

    record = _make_record(repo, batch_size=10)
    summaries = await record.execute()

    per_market = [s for s in summaries if isinstance(s, RecordResultSummary)]
    cycle = [s for s in summaries if isinstance(s, RecordResultCycleSummary)]
    assert len(per_market) == 10  # batch membatasi
    assert len(cycle) == 1
    assert cycle[0].processed == 10
    assert cycle[0].resolved == 10
    assert repo.pending_limit_seen == [10]  # query DB dibatasi batch


async def test_batch_sisa_diproses_siklus_berikutnya() -> None:
    repo = _FakeRepo()
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    repo.pending = [
        _pending(
            i,
            f"btc-updown-5m-{int((start + timedelta(minutes=5 * i)).timestamp())}",
        )
        for i in range(15)
    ]

    record = _make_record(repo, batch_size=10)
    first = await record.execute()
    second = await record.execute()  # hasil pertama sudah terarsip

    assert sum(isinstance(s, RecordResultSummary) for s in first) == 10
    per_market = [s for s in second if isinstance(s, RecordResultSummary)]
    assert len(per_market) == 5  # sisa 5 diproses siklus kedua


async def test_default_batch_size_sepuluh() -> None:
    assert DEFAULT_BATCH_SIZE == 10
    assert DEFAULT_SWEEP_BATCH == 50


async def test_sweep_batch_membatasi_kandidat() -> None:
    repo = _FakeRepo()
    start = datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    repo.stale_open = [
        StaleMarket(
            market_id=i,
            condition_id=f"0x{i}",
            slug=f"btc-updown-5m-{int((start + timedelta(minutes=5 * i)).timestamp())}",
            status=MarketStatus.LIVE,
            end_at=start + timedelta(minutes=5 * (i + 1)),
        )
        for i in range(80)  # lebih dari default 50
    ]

    sweep = SweepMarketStatus(repo, FakeClock(NOW + timedelta(hours=12)))
    summary = await sweep.execute()

    assert summary.scanned == 50  # dibatasi batch
    assert summary.closed == 50
    assert summary.duration_ms >= 0


# ------------------------------------------------------------------ ambang adaptif


class _NoActivity:
    async def live_market_count(self) -> int:
        return 0

    async def last_trade_at_live(self) -> datetime | None:
        return None


class _NoopNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append(text)


def _make_monitor(
    clock: FakeClock,
    *,
    stale_discover_s: float = DEFAULT_STALE_DISCOVER_SEC,
) -> HealthMonitor:
    return HealthMonitor(
        activity=_NoActivity(),
        notifier=_NoopNotifier(),
        clock=clock,
        stale_ingest_s=600.0,
        stale_discover_s=stale_discover_s,
        grace_s=0.0,  # langsung evaluasi tanpa grace
    )


async def test_ambang_adaptif_naik_dengan_durasi_siklus() -> None:
    """Siklus terukur 200 dtk → ambang efektif max(3x200, 300) = 600 dtk."""
    clock = FakeClock(NOW)
    monitor = _make_monitor(clock)

    assert monitor.effective_stale_s("discover") == DEFAULT_STALE_DISCOVER_SEC  # floor

    await monitor.record_cycle("discover", ok=True, duration_s=200.0)
    expected = max(ADAPTIVE_STALE_MULTIPLIER * 200.0, DEFAULT_STALE_DISCOVER_SEC)
    assert monitor.effective_stale_s("discover") == expected  # 600 > floor 300


async def test_ambang_adaptif_tidak_turun_di_bawah_floor() -> None:
    """Siklus cepat 5 dtk → ambang tetap floor 300 dtk (bukan 15 dtk)."""
    clock = FakeClock(NOW)
    monitor = _make_monitor(clock)

    await monitor.record_cycle("discover", ok=True, duration_s=5.0)
    assert monitor.effective_stale_s("discover") == DEFAULT_STALE_DISCOVER_SEC


async def test_ambang_adaptif_mencegah_alarm_palsu() -> None:
    """Siklus 250 dtk selesai teratur tiap 60 dtk — tidak boleh stale.

    Tanpa ambang adaptif (floor 300), siklus berjalan baik-baik saja namun
    gap antar-aktivitas 300+ dtk memicu alarm palsu (BUG-011).
    """
    clock = FakeClock(NOW)
    monitor = _make_monitor(clock, stale_discover_s=300.0)

    # Siklus terukur 250 dtk → ambang efektif 750 dtk.
    await monitor.record_cycle("discover", ok=True, duration_s=250.0)
    # Waktu berjalan 700 dtk tanpa siklus baru (masih < 750) → tidak stale.
    clock._now = NOW + timedelta(seconds=700)
    report = await monitor.check()
    discover = next(c for c in report.components if c.name == "discover")
    assert discover.stale is False

    # Lewat 750 dtk → stale (deteksi asli tetap berfungsi).
    clock._now = NOW + timedelta(seconds=800)
    report = await monitor.check()
    discover = next(c for c in report.components if c.name == "discover")
    assert discover.stale is True


async def test_durasi_siklus_dikirim_ke_monitor_dari_scheduler() -> None:
    """Scheduler melaporkan durasi — record_cycle menerima duration_s."""
    clock = FakeClock(NOW)
    monitor = _make_monitor(clock)
    await monitor.record_cycle("result", ok=True, duration_s=42.5)
    assert monitor.effective_stale_s("result") == max(3.0 * 42.5, 300.0)


async def test_ingest_tidak_adaptif() -> None:
    clock = FakeClock(NOW)
    monitor = _make_monitor(clock)
    await monitor.record_cycle("ingest", ok=True, duration_s=999.0)
    assert monitor.effective_stale_s("ingest") == 600.0  # statis


# ------------------------------------------------------------------ cache negatif


async def test_cache_negatif_aset_tanpa_market() -> None:
    """Aset tanpa satu pun market tidak di-query ulang pada siklus kedua."""
    repo = _FakeRepo()

    class _PartialSource(_ConcurrencyProbeSource):
        """btc punya market (jendela berjalan), xag tidak sama sekali."""

        def __init__(self) -> None:
            super().__init__(delay_s=0.0)

        async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
            await super().fetch_by_slug(slug, closed=closed)
            if slug.startswith("btc"):
                return _snapshot(slug, f"cond-{slug}")
            return None

    source = _PartialSource()
    mono = _FakeMonotonic()
    discover = _make_discover(source, repo, assets=("btc", "xag"), monotonic=mono)

    first = await discover.execute()
    assert first.dead_asset_skips == 0
    calls_cycle_1 = len(source.calls)
    assert calls_cycle_1 == 2 * 2 * 6  # 2 aset x 2 timeframe x 6 jendela
    assert first.total_markets > 0  # btc menemukan market

    summary = await discover.execute()
    assert summary.dead_asset_skips == 1  # xag di-skip siklus kedua
    assert len(source.calls) == calls_cycle_1 + 12  # hanya btc (12 jendela) lagi


async def test_cache_negatif_kedaluwarsa_setelah_ttl() -> None:
    """Setelah TTL lewat, aset mati di-query ulang (bisa jadi hidup lagi)."""
    repo = _FakeRepo()
    source = _ConcurrencyProbeSource(delay_s=0.0)
    mono = _FakeMonotonic()
    discover = _make_discover(source, repo, assets=("xag",), monotonic=mono, dead_asset_ttl_s=600.0)

    await discover.execute()
    calls_after_cycle_1 = len(source.calls)
    mono.advance(601.0)  # TTL habis

    summary = await discover.execute()
    assert summary.dead_asset_skips == 0  # di-query ulang
    assert len(source.calls) == calls_after_cycle_1 + 12  # 2 tf x 6 jendela


async def test_aset_hidup_kembali_dihapus_dari_cache() -> None:
    """Aset yang tadinya mati tapi menemukan market di-query normal lagi."""
    repo = _FakeRepo()

    class _ReviveSource(_ConcurrencyProbeSource):
        def __init__(self) -> None:
            super().__init__(delay_s=0.0)
            self.revived = False

        async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
            if slug.startswith("xag") and self.revived:
                return _snapshot(slug, f"cond-{slug}")
            return await super().fetch_by_slug(slug, closed=closed)

    revive = _ReviveSource()
    mono = _FakeMonotonic()
    discover = _make_discover(revive, repo, assets=("xag",), monotonic=mono)

    first = await discover.execute()  # xag mati → cache negatif
    assert first.dead_asset_skips == 0  # masih di-query siklus pertama

    mono.advance(601.0)  # TTL habis → xag di-query ulang
    revive.revived = True
    second = await discover.execute()
    assert second.dead_asset_skips == 0
    assert second.total_markets > 0  # market xag ditemukan

    third = await discover.execute()  # tidak lagi dianggap mati
    assert third.dead_asset_skips == 0


async def test_dua_aset_mati_diskip_keduanya() -> None:
    repo = _FakeRepo()
    source = _ConcurrencyProbeSource(delay_s=0.0)
    mono = _FakeMonotonic()
    discover = _make_discover(source, repo, assets=("xag", "xau"), monotonic=mono)

    await discover.execute()
    summary = await discover.execute()
    assert summary.dead_asset_skips == 2


# ------------------------------------------------------------------ duration_ms


async def test_discover_mencatat_duration_ms() -> None:
    repo = _FakeRepo()
    source = _ConcurrencyProbeSource(delay_s=0.0)
    mono = _FakeMonotonic()
    discover = _make_discover(source, repo, monotonic=mono)

    class _StepMonotonic(_FakeMonotonic):
        """Naik 1.5 dtk per panggilan — durasi terukur 3000 ms."""

        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        def __call__(self) -> float:
            self._t = self._calls * 1.5
            self._calls += 1
            return self._t

    discover.monotonic = _StepMonotonic()
    summary = await discover.execute()
    assert summary.duration_ms == 3000


async def test_record_result_mencatat_duration_ms() -> None:
    repo = _FakeRepo()
    record = _make_record(repo, batch_size=10)
    summaries = await record.execute()
    cycle = next(s for s in summaries if isinstance(s, RecordResultCycleSummary))
    assert isinstance(cycle.duration_ms, int)
    assert cycle.duration_ms >= 0


@pytest.mark.parametrize("field", ["gamma_max_concurrency", "result_batch_size"])
def test_settings_baru_t_029(field: str) -> None:
    from polywatch.main.config import Settings

    env = {
        "BOT_TOKEN": "t",
        "OWNER_CHAT_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    }
    settings = Settings.model_validate(env)
    assert getattr(settings, field) == (5 if field == "gamma_max_concurrency" else 10)


def test_settings_interval_default_60() -> None:
    from polywatch.main.config import Settings

    env = {
        "BOT_TOKEN": "t",
        "OWNER_CHAT_ID": "1",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    }
    settings = Settings.model_validate(env)
    assert settings.discover_interval_s == 60.0
    assert settings.result_interval_s == 60.0
    assert settings.stale_discover_s == 300.0
    assert settings.stale_result_s == 300.0
