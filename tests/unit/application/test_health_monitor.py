"""Unit test HealthMonitor (T-019 + T-027) — tanpa tidur, jam palsu deterministik.

Membuktikan (T-019): stale → tepat 1 notifikasi; masih stale < rate limit →
tidak kirim lagi; stale melewati rate limit → kirim ulang; pulih →
notifikasi pemulihan; grace period; siklus gagal tidak dihitung aktivitas;
notifikasi gagal kirim tidak melempar.

Membuktikan (T-027):
- kesegaran ingest = heartbeat `record_cycle("ingest", ok)` — jendela sepi
  (tidak ada trade baru) dengan heartbeat aktif → TIDAK alarm;
- heartbeat benar-benar berhenti melewati ambang → alarm dengan angka nyata;
- tanpa market LIVE → tidak ada alarm ingest;
- restart dengan state persisten tidak mengirim ulang alarm (< 10 menit);
- pesan alarm menyebut menit + batch terakhir + market live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polywatch.application.use_cases.health_monitor import (
    DEFAULT_STALE_DISCOVER_SEC,
    CycleKind,
    HealthMonitor,
)


class FakeClock:
    """Jam yang bisa dimajukan manual."""

    def __init__(self) -> None:
        self._now = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeActivity:
    """Repository aktivitas palsu — trade terakhir + jumlah market live."""

    def __init__(
        self,
        last_trade_at: datetime | None = None,
        live_markets: int = 17,
    ) -> None:
        self.last_trade_at = last_trade_at
        self.live_markets = live_markets

    async def last_trade_at_live(self) -> datetime | None:
        return self.last_trade_at

    async def live_market_count(self) -> int:
        return self.live_markets


class FakeNotifier:
    """Menangkap semua pesan yang 'terkirim'."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append(text)

    def stale_alerts(self) -> list[str]:
        return [t for t in self.sent if t.startswith("⚠️")]

    def recovery_alerts(self) -> list[str]:
        return [t for t in self.sent if t.startswith("✅")]


class FailingNotifier:
    """Notifier yang selalu melempar — tidak boleh menghentikan monitor."""

    async def send(self, text: str, chat_id: int | None = None) -> None:
        raise RuntimeError("telegram down")


class MemoryStateStore:
    """Store state notifikasi in-memory (implementasi port NotificationStore)."""

    def __init__(self) -> None:
        self.records: dict[str, tuple[datetime | None, str | None]] = {}

    def load(self) -> dict[str, tuple[datetime | None, str | None]]:
        return dict(self.records)

    def save(self, records: dict[str, tuple[datetime | None, str | None]]) -> None:
        self.records = dict(records)


def _monitor(
    activity: FakeActivity,
    notifier: FakeNotifier,
    clock: FakeClock,
    *,
    stale_ingest_s: float = 600.0,
    rate_limit_s: float = 600.0,
    grace_s: float = 300.0,
    state_store: MemoryStateStore | None = None,
) -> HealthMonitor:
    return HealthMonitor(
        activity=activity,
        notifier=notifier,
        clock=clock,
        stale_ingest_s=stale_ingest_s,
        stale_discover_s=DEFAULT_STALE_DISCOVER_SEC,
        rate_limit_s=rate_limit_s,
        grace_s=grace_s,
        state_store=state_store,
    )


# ---------------------------------------------------------------------------
# T-027: heartbeat ingest, bukan trade market LIVE
# ---------------------------------------------------------------------------


async def test_jendela_sepi_heartbeat_aktif_tidak_alarm() -> None:
    """Pasar sepi (tidak ada trade baru) + heartbeat ingest aktif → tanpa alarm.

    Ini reproduksi inti keluhan owner: sebelumnya ingest diukur dari
    `last_trade_at_live` — jendela 5m baru tanpa trade → alarm palsu.
    """
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)  # sepi
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    # Discover/result sehat + heartbeat ingest aktif (batch commit datang).
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)

    # Cek berkala selama 10 menit dengan semua komponen tetap aktif.
    for _ in range(20):
        clock.advance(30)
        await monitor.record_cycle("discover", ok=True)
        await monitor.record_cycle("result", ok=True)
        await monitor.record_cycle("ingest", ok=True)  # heartbeat aktif
        report = await monitor.check()
        assert report.stale_names == ()

    assert notifier.sent == []


async def test_heartbeat_berhenti_melewati_ambang_memicu_alarm() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)
    report = await monitor.check()
    assert report.stale_names == ()

    # Heartbeat berhenti total.
    clock.advance(601)  # melewati ambang 600 dtk
    await monitor.record_cycle("discover", ok=True)  # scheduler tetap sehat
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()

    assert report.stale_names == ("ingest",)
    assert len(notifier.stale_alerts()) == 1
    msg = notifier.stale_alerts()[0]
    assert "heartbeat" in msg
    assert "10 menit" in msg  # angka nyata dari ambang
    assert "market live: 17" in msg


async def test_tanpa_market_live_tidak_alarm_ingest() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=0)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    # Tidak ada heartbeat ingest sama sekali + tanpa market live.
    clock.advance(2000)
    report = await monitor.check()

    assert "ingest" not in report.stale_names
    ingest = next(c for c in report.components if c.name == "ingest")
    assert not ingest.stale
    assert not any("heartbeat" in t for t in notifier.stale_alerts())


async def test_pesan_alarm_ingest_pakai_format_baru() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=3)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    clock.advance(601)
    await monitor.check()

    msg = notifier.stale_alerts()[0]
    assert msg.startswith("⚠️ polywatch: tidak ada heartbeat ingest")
    assert "batch terakhir" in msg
    assert "market live: 3" in msg


# ---------------------------------------------------------------------------
# T-019: perilaku dasar (disesuaikan ke heartbeat ingest)
# ---------------------------------------------------------------------------


async def test_stale_ingest_memicu_satu_notifikasi() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)

    clock.advance(601)  # heartbeat basi
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()

    assert report.stale_names == ("ingest",)
    assert len(notifier.stale_alerts()) == 1
    assert "heartbeat" in notifier.stale_alerts()[0]


async def test_masih_stale_di_bawah_rate_limit_tidak_kirim_lagi() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0, rate_limit_s=600.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)

    clock.advance(601)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()  # notifikasi pertama
    clock.advance(300)  # 5 menit kemudian — masih di bawah 10 menit
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()

    assert report.stale_names == ("ingest",)
    assert len(notifier.sent) == 1  # tidak ada pesan baru


async def test_stale_melewati_rate_limit_kirim_ulang() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0, rate_limit_s=600.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)

    clock.advance(601)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()
    clock.advance(601)  # lewat rate limit 10 menit
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()

    assert len(notifier.stale_alerts()) == 2  # pengingat kedua terkirim


async def test_pulih_mengirim_notifikasi_pemulihan() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)

    clock.advance(601)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()
    assert len(notifier.stale_alerts()) == 1

    # Heartbeat ingest kembali (batch commit datang) → pulih.
    await monitor.record_cycle("ingest", ok=True)
    report = await monitor.check()

    assert report.stale_names == ()
    assert len(notifier.recovery_alerts()) == 1
    assert "ingest" in notifier.recovery_alerts()[0]

    # Check berikutnya (masih sehat) tidak mengirim apa pun.
    await monitor.record_cycle("ingest", ok=True)
    await monitor.check()
    assert len(notifier.sent) == 2


async def test_pemulihan_lewat_record_cycle_siklus_sukses() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    # Discover basi (tidak pernah siklus sukses) lewat grace period.
    clock.advance(1000)
    await monitor.check()
    assert any("discover" in t for t in notifier.stale_alerts())

    # Siklus discover sukses → pemulihan + aktivitas tercatat.
    await monitor.record_cycle("discover", ok=True)
    report = await monitor.check()
    assert "discover" not in report.stale_names
    assert any("discover" in t for t in notifier.recovery_alerts())


async def test_tanpa_data_saat_start_grace_period_tidak_alarm() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=300.0)

    report = await monitor.check()
    clock.advance(120)  # masih dalam grace period
    report = await monitor.check()

    assert report.stale_names == ()
    assert notifier.sent == []

    # Setelah grace period lewat tanpa data → alarm.
    clock.advance(301)
    report = await monitor.check()
    assert "ingest" in report.stale_names
    assert notifier.stale_alerts()


async def test_siklus_gagal_tidak_dihitung_aktivitas() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    await monitor.record_cycle("discover", ok=False)  # gagal — bukan aktivitas
    clock.advance(400)  # melewati ambang discover 180 dtk
    report = await monitor.check()

    assert "discover" in report.stale_names


async def test_notifier_gagal_tidak_melempar() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    monitor = HealthMonitor(
        activity=activity,
        notifier=FailingNotifier(),
        clock=clock,
        stale_ingest_s=600.0,
        grace_s=0.0,
    )
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)
    clock.advance(601)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()  # tidak boleh raise
    assert report.stale_names == ("ingest",)


async def test_komponen_sehat_dari_awal_tanpa_pesan() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now(), live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock)

    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)
    report = await monitor.check()

    assert report.stale_names == ()
    assert notifier.sent == []
    for component in report.components:
        assert not component.notified


async def test_dua_komponen_stale_dinotifikasi_terpisah() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    report = await monitor.check()

    # ingest (heartbeat tidak pernah datang) + discover & result (belum ada
    # siklus) — tiga-tiganya basi, dinotifikasi terpisah.
    assert set(report.stale_names) == {"ingest", "discover", "result"}
    assert len(notifier.stale_alerts()) == 3


@pytest.mark.parametrize("kind", ["discover", "result", "ingest"])
async def test_record_cycle_sukses_menghapus_status_stale(kind: CycleKind) -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    clock.advance(1000)  # semua komponen basi
    report = await monitor.check()
    assert kind in report.stale_names

    await monitor.record_cycle(kind, ok=True)
    report = await monitor.check()
    assert kind not in report.stale_names


# ---------------------------------------------------------------------------
# T-027: idempoten antar-restart (state notifikasi persisten)
# ---------------------------------------------------------------------------


async def test_restart_dalam_cooldown_tidak_kirim_ulang_alarm() -> None:
    """Gangguan berlangsung, alarm terkirim, proses restart < 10 menit →
    monitor baru (dengan state store) tidak mengirim alarm lagi."""
    clock = FakeClock()
    store = MemoryStateStore()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier1 = FakeNotifier()
    monitor1 = _monitor(activity, notifier1, clock, grace_s=0.0, state_store=store)
    await monitor1.record_cycle("discover", ok=True)
    await monitor1.record_cycle("result", ok=True)
    await monitor1.record_cycle("ingest", ok=True)
    clock.advance(601)
    await monitor1.record_cycle("discover", ok=True)
    await monitor1.record_cycle("result", ok=True)
    await monitor1.check()
    assert len(notifier1.stale_alerts()) == 1

    # Restart 5 menit kemudian: monitor baru + state dari disk.
    clock.advance(300)
    notifier2 = FakeNotifier()
    monitor2 = _monitor(activity, notifier2, clock, grace_s=0.0, state_store=store)
    await monitor2.record_cycle("discover", ok=True)
    await monitor2.record_cycle("result", ok=True)
    report = await monitor2.check()

    assert "ingest" in report.stale_names  # masih basi —
    assert notifier2.stale_alerts() == []  # — tapi TANPA alarm ulang (cooldown)


async def test_restart_melewati_cooldown_kirim_lagi() -> None:
    clock = FakeClock()
    store = MemoryStateStore()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier1 = FakeNotifier()
    monitor1 = _monitor(activity, notifier1, clock, grace_s=0.0, state_store=store)
    await monitor1.record_cycle("discover", ok=True)
    await monitor1.record_cycle("result", ok=True)
    await monitor1.record_cycle("ingest", ok=True)
    clock.advance(601)
    await monitor1.record_cycle("discover", ok=True)
    await monitor1.record_cycle("result", ok=True)
    await monitor1.check()
    assert len(notifier1.stale_alerts()) == 1

    # Restart setelah cooldown lewat → boleh kirim pengingat.
    clock.advance(601)
    notifier2 = FakeNotifier()
    monitor2 = _monitor(activity, notifier2, clock, grace_s=0.0, state_store=store)
    await monitor2.record_cycle("discover", ok=True)
    await monitor2.record_cycle("result", ok=True)
    await monitor2.check()

    assert len(notifier2.stale_alerts()) == 1


async def test_heartbeat_sehat_menulis_state_ok() -> None:
    clock = FakeClock()
    store = MemoryStateStore()
    activity = FakeActivity(last_trade_at=None, live_markets=17)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0, state_store=store)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.record_cycle("ingest", ok=True)
    clock.advance(601)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()

    assert store.load()["health:ingest"] == (
        clock.now(),
        "stale",
    )

    await monitor.record_cycle("ingest", ok=True)
    assert store.load()["health:ingest"][1] == "ok"


# ---------------------------------------------------------------------------
# T-027: JsonHealthStateStore (adapter file JSON)
# ---------------------------------------------------------------------------


def test_json_state_store_roundtrip(tmp_path: Path) -> None:
    from polywatch.main.health_state import JsonHealthStateStore

    path = str(tmp_path / "health-state.json")
    store = JsonHealthStateStore(path)
    assert store.load() == {}

    when = datetime(2026, 11, 8, 12, 31, 20, tzinfo=UTC)
    store.save({"health:ingest": (when, "stale")})
    assert store.load() == {"health:ingest": (when, "stale")}

    store.save({"health:ingest": (None, "ok")})
    assert store.load() == {"health:ingest": (None, "ok")}


def test_json_state_store_file_corrupt_mulai_kosong(tmp_path: Path) -> None:
    from polywatch.main.health_state import JsonHealthStateStore

    path = tmp_path / "health-state.json"
    path.write_text("bukan json {", encoding="utf-8")
    store = JsonHealthStateStore(str(path))
    assert store.load() == {}


def test_json_state_store_tak_tertulis_tidak_melempar(tmp_path: Path) -> None:
    from polywatch.main.health_state import JsonHealthStateStore

    # Path di direktori file (bukan direktori) → OSError saat menulis.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = JsonHealthStateStore(str(blocker / "state.json"))
    store.save({"health:ingest": (None, "stale")})  # tidak boleh raise
