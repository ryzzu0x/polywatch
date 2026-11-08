"""Unit test HealthMonitor (T-019) — tanpa tidur, jam palsu deterministik.

Membuktikan: stale → tepat 1 notifikasi; masih stale < rate limit → tidak
kirim lagi; stale melewati rate limit → kirim ulang; pulih → notifikasi
pemulihan (via check maupun record_cycle); tanpa data saat start → grace
period (tidak langsung alarm); siklus gagal tidak dihitung aktivitas;
notifikasi gagal kirim tidak melempar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    """Repository aktivitas palsu — trade terakhir bisa diatur per test."""

    def __init__(self, last_trade_at: datetime | None) -> None:
        self.last_trade_at = last_trade_at
        self.calls = 0

    async def last_trade_at_live(self) -> datetime | None:
        self.calls += 1
        return self.last_trade_at


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


def _monitor(
    activity: FakeActivity,
    notifier: FakeNotifier,
    clock: FakeClock,
    *,
    stale_ingest_s: float = 120.0,
    rate_limit_s: float = 600.0,
    grace_s: float = 300.0,
) -> HealthMonitor:
    return HealthMonitor(
        activity=activity,
        notifier=notifier,
        clock=clock,
        stale_ingest_s=stale_ingest_s,
        stale_discover_s=DEFAULT_STALE_DISCOVER_SEC,
        rate_limit_s=rate_limit_s,
        grace_s=grace_s,
    )


async def test_stale_ingest_memicu_satu_notifikasi() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(seconds=121))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)
    # Discover & result sehat supaya hanya ingest yang basi.
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)

    report = await monitor.check()

    assert report.stale_names == ("ingest",)
    assert len(notifier.stale_alerts()) == 1
    assert "trade" in notifier.stale_alerts()[0].lower()


async def test_masih_stale_di_bawah_rate_limit_tidak_kirim_lagi() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(hours=1))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0, rate_limit_s=600.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)

    await monitor.check()  # notifikasi pertama
    clock.advance(300)  # 5 menit kemudian — masih di bawah 10 menit
    await monitor.record_cycle("discover", ok=True)  # sehat di titik waktu baru
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()

    assert report.stale_names == ("ingest",)
    assert len(notifier.sent) == 1  # tidak ada pesan baru


async def test_stale_melewati_rate_limit_kirim_ulang() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(hours=1))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0, rate_limit_s=600.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)

    await monitor.check()
    # Cicilan sehat baru setelah maju waktu supaya discover/result tidak ikut basi.
    clock.advance(601)  # lewat rate limit 10 menit
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    await monitor.check()

    assert len(notifier.stale_alerts()) == 2  # pengingat kedua terkirim


async def test_pulih_mengirim_notifikasi_pemulihan() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(hours=1))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)

    await monitor.check()
    assert len(notifier.stale_alerts()) == 1

    activity.last_trade_at = clock.now()  # trade baru masuk → pulih
    report = await monitor.check()

    assert report.stale_names == ()
    assert len(notifier.recovery_alerts()) == 1
    assert "ingest" in notifier.recovery_alerts()[0]

    # Check berikutnya (masih sehat) tidak mengirim apa pun.
    activity.last_trade_at = clock.now()
    await monitor.check()
    assert len(notifier.sent) == 2


async def test_pemulihan_lewat_record_cycle_siklus_sukses() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=None)  # ingest tanpa data
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
    activity = FakeActivity(last_trade_at=None)
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
    activity = FakeActivity(last_trade_at=None)
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    await monitor.record_cycle("discover", ok=False)  # gagal — bukan aktivitas
    clock.advance(400)  # melewati ambang discover 180 dtk
    report = await monitor.check()

    assert "discover" in report.stale_names


async def test_notifier_gagal_tidak_melempar() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(hours=1))
    monitor = HealthMonitor(
        activity=activity,
        notifier=FailingNotifier(),
        clock=clock,
        stale_ingest_s=120.0,
        grace_s=0.0,
    )
    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()  # tidak boleh raise
    assert report.stale_names == ("ingest",)


async def test_komponen_sehat_dari_awal_tanpa_pesan() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(seconds=10))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock)

    await monitor.record_cycle("discover", ok=True)
    await monitor.record_cycle("result", ok=True)
    report = await monitor.check()

    assert report.stale_names == ()
    assert notifier.sent == []
    assert activity.calls == 1
    for component in report.components:
        assert not component.notified


async def test_dua_komponen_stale_dinotifikasi_terpisah() -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now() - timedelta(hours=1))
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    report = await monitor.check()

    # ingest (trade basi) + discover & result (belum ada siklus) — tiga-tiganya.
    assert set(report.stale_names) == {"ingest", "discover", "result"}
    assert len(notifier.stale_alerts()) == 3


@pytest.mark.parametrize("kind", ["discover", "result"])
async def test_record_cycle_sukses_menghapus_status_stale(kind: CycleKind) -> None:
    clock = FakeClock()
    activity = FakeActivity(last_trade_at=clock.now())
    notifier = FakeNotifier()
    monitor = _monitor(activity, notifier, clock, grace_s=0.0)

    clock.advance(1000)  # semua komponen basi
    report = await monitor.check()
    assert kind in report.stale_names

    await monitor.record_cycle(kind, ok=True)
    report = await monitor.check()
    assert kind not in report.stale_names
