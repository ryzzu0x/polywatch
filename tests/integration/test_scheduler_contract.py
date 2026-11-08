"""Integration test kontrak siklus scheduler lewat Container nyata (T-025).

Reproduksi bug T-024: container menyimpan *fungsi coroutine* sementara
scheduler memanggil `.execute()` — setiap siklus gagal `AttributeError`
yang hanya di-log (`scheduler_cycle_error`) sehingga discovery tidak
pernah jalan dan tabel `markets` tetap 0. Field container bertipe `Any`
membuat mypy buta terhadap ketidakcocokan ini.

Test ini mengambil objek scheduler-ready **dari Container yang dirakit
nyata** (`setup_scheduler`, bukan fake) dan memastikan bentuknya sesuai
kontrak scheduler (ADR-011): `inspect.iscoroutinefunction`, lalu loop
scheduler dengan interval milidetik berjalan ≥1 siklus tanpa
`scheduler_cycle_error`, dan market live tersimpan ke DB (count > 0).

Gamma di-mock lewat respx (format respons nyata: list of payload); DB
nyata (`TEST_DATABASE_URL`, polywatch_test). Auto-skip bila env tidak
di-set.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.application.use_cases.discover_markets import build_slug, window_start
from polywatch.domain.market import Timeframe
from polywatch.infrastructure.db.models import Base, MarketModel
from polywatch.main.config import Settings
from polywatch.main.container import Container
from polywatch.main.scheduler import cancel_all, create_scheduler_tasks

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; integration test DB di-skip",
)

GAMMA = "https://gamma-api.polymarket.com"
FIXTURE = Path("docs/spikes/t005-gamma-market-sample.json")

_TEST_ENV = {
    "BOT_TOKEN": "123456:TEST-token-format-valid",
    "OWNER_CHAT_ID": "1",
    "DATABASE_URL": TEST_DSN or "",
    "POLYMARKET_GAMMA_URL": GAMMA,
    "MARKET_ASSETS": "btc,",
    "DISCOVER_INTERVAL_S": "0.05",
    "RESULT_INTERVAL_S": "0.05",
}


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DSN or "")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


def _live_payload(slug: str) -> dict[str, object]:
    """Payload market live (jendela 5m berjalan/berikutnya) berformat Gamma nyata."""
    template: dict[str, object] = json.loads(FIXTURE.read_text())["response_truncated"]
    return {
        "slug": slug,
        "conditionId": f"{slug}-cond",
        "clobTokenIds": json.dumps([f"{slug}-up", f"{slug}-down"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "volume": str(template.get("volume") or "0"),
        "closed": False,
    }


def _slug_5m(now: datetime, offset: int) -> str:
    return build_slug(
        "btc", Timeframe.FIVE_MINUTES, window_start(now, Timeframe.FIVE_MINUTES, offset)
    )


async def _count(engine: AsyncEngine, model: type) -> int:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_container_siklus_sesuai_kontrak_scheduler_callable() -> None:
    """Siklus dari container nyata harus callable coroutine — bukan objek .execute()."""
    settings = Settings.model_validate({**_TEST_ENV, "MARKET_ASSETS": ("btc",)})
    container = Container.build(settings)
    try:
        container.setup_scheduler()
        assert container.discover_cycle is not None
        assert container.record_result_cycle is not None
        assert container.recompute_cycle is not None
        # Kontrak (ADR-011): callable coroutine — inilah yang dipanggil scheduler.
        assert inspect.iscoroutinefunction(container.discover_cycle)
        assert inspect.iscoroutinefunction(container.record_result_cycle)
        assert inspect.iscoroutinefunction(container.recompute_cycle)
    finally:
        await container.aclose()


async def test_scheduler_lewat_container_jalan_tanpa_cycle_error(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Loop scheduler + container nyata: market tersimpan, tanpa scheduler_cycle_error.

    Ini test regresi T-025 — sebelum perbaikan, `discover.execute` di
    scheduler melempar AttributeError pada closure container sehingga
    `markets` tetap 0 meski loop berjalan.
    """
    now = datetime.now(tz=UTC)
    live = [_live_payload(_slug_5m(now, 0)), _live_payload(_slug_5m(now, 1))]

    with respx.mock(base_url=GAMMA) as mock:
        # Endpoint /markets dipakai jalur slug & list (record_result: market
        # CLOSED tidak ada → daftar kosong pun cukup; asalkan tidak error).
        mock.get("/markets").mock(return_value=httpx.Response(200, json=live))
        settings = Settings.model_validate({**_TEST_ENV, "MARKET_ASSETS": ("btc",)})
        container = Container.build(settings)
        stop = asyncio.Event()
        caplog.set_level(logging.ERROR, logger="polywatch.scheduler")
        try:
            container.setup_scheduler()
            assert container.discover_cycle is not None
            assert container.record_result_cycle is not None
            assert container.recompute_cycle is not None
            tasks = create_scheduler_tasks(
                container.discover_cycle,
                container.record_result_cycle,
                container.recompute_cycle,
                settings,
                stop,
            )
            await asyncio.sleep(1.5)
            stop.set()
            await cancel_all(tasks)
        finally:
            await container.aclose()

    errors = [r for r in caplog.records if getattr(r, "event", None) == "scheduler_cycle_error"]
    assert not errors, f"scheduler_cycle_error muncul: {[r.getMessage() for r in errors]}"
    saved = await _count(engine, MarketModel)
    assert saved > 0, "market live seharusnya tersimpan lewat jalur container"


def test_contract_symbol_volume_decimal() -> None:
    """Jaga format payload mock tetap sesuai parser Gamma (bukan test produk)."""
    payload = _live_payload(_slug_5m(datetime.now(tz=UTC), 0))
    tokens = json.loads(str(payload["clobTokenIds"]))
    assert len(tokens) == 2
    assert Decimal(str(payload["volume"])) >= 0


async def test_monitor_record_cycle_diterima_lewat_container_urutan_cli(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """T-027 regresi urutan perakitan: setup_health SEBELUM setup_scheduler.

    Sebelum perbaikan, CLI memanggil `setup_scheduler()` dulu — saat task
    scheduler dibuat `container.health_monitor` masih None → `record_cycle`
    tidak pernah dipanggil → discover/result selalu "belum ada aktivitas"
    → alarm palsu. Test ini merakit container dengan URUTAN CLI (health →
    scheduler → ingest) dan membuktikan `record_cycle` benar-benar terpanggil
    setelah ≥1 siklus scheduler.
    """
    now = datetime.now(tz=UTC)
    live = [_live_payload(_slug_5m(now, 0)), _live_payload(_slug_5m(now, 1))]

    class _RecordingMonitor:
        """Perekam panggilan record_cycle — dipasang setelah setup_health."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def record_cycle(self, kind: str, ok: bool, duration_s: float | None = None) -> None:
            self.calls.append((kind, ok))

    with respx.mock(base_url=GAMMA) as mock:
        mock.get("/markets").mock(return_value=httpx.Response(200, json=live))
        settings = Settings.model_validate({**_TEST_ENV, "MARKET_ASSETS": ("btc",)})
        container = Container.build(settings)
        stop = asyncio.Event()
        recorder = _RecordingMonitor()
        try:
            # Urutan CLI (T-027): health duluan, scheduler belakangan.
            container.setup_health()
            assert container.health_monitor is not None
            container.setup_scheduler()
            # Ganti monitor dengan perekam — container field yang sama yang
            # dibaca runner/scheduler, membuktikan wiring pakai objek hidup.
            container.health_monitor = recorder  # type: ignore[assignment]
            assert container.discover_cycle is not None
            assert container.record_result_cycle is not None
            assert container.recompute_cycle is not None
            tasks = create_scheduler_tasks(
                container.discover_cycle,
                container.record_result_cycle,
                container.recompute_cycle,
                settings,
                stop,
                monitor=container.health_monitor,
            )
            # Discover butuh ~2 dtk (throttle Gamma ≤5 req/s pada 1 aset
            # x 6 jendela + supplement) — beri waktu ≥1 siklus discover.
            await asyncio.sleep(3.0)
            stop.set()
            await cancel_all(tasks)
        finally:
            await container.aclose()

    kinds = {kind for kind, _ok in recorder.calls}
    assert "discover" in kinds, "record_cycle('discover', ...) harus terpanggil"
    assert "result" in kinds, "record_cycle('result', ...) harus terpanggil"
    assert all(ok for _kind, ok in recorder.calls), "siklus mock harus sukses"


async def test_heartbeat_ingest_diteruskan_ke_monitor_container(
    engine: AsyncEngine,
) -> None:
    """T-027: on_heartbeat IngestTrades lewat container → record_cycle("ingest").

    Heartbeat ingest (bukan trade LIVE) adalah dasar kesegaran ingest
    (ADR-014) — test membuktikan container meneruskannya ke monitor.
    """
    settings = Settings.model_validate({**_TEST_ENV, "MARKET_ASSETS": ("btc",)})
    container = Container.build(settings)

    class _RecordingMonitor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def record_cycle(self, kind: str, ok: bool, duration_s: float | None = None) -> None:
            self.calls.append((kind, ok))

    recorder = _RecordingMonitor()
    container.health_monitor = recorder  # type: ignore[assignment]
    try:
        await container._report_ingest_heartbeat(True)
        await container._report_ingest_heartbeat(False)
    finally:
        await container.aclose()

    assert recorder.calls == [("ingest", True), ("ingest", False)]
