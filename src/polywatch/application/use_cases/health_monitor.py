"""Use case health_monitor: deteksi aktivitas basi + notifikasi owner (T-019).

Kesegaran tiga komponen diperiksa setiap `check()` (dipanggil dari runner,
default tiap `HEALTH_CHECK_INTERVAL_S` = 30 dtk):

- `ingest` — trade terakhir pada market LIVE (port `HealthActivityRepository`);
- `discover` / `result` — siklus scheduler terakhir yang berhasil, dilaporkan
  lewat `record_cycle` dari loop scheduler.

Ambang default `STALE_INGEST_S` = 120 dtk dan `STALE_DISCOVER_S` = 180 dtk.
Komponen basi memicu satu notifikasi owner via port `Notifier`, maksimal satu
per jenis per `rate_limit_s` (default 600 dtk / 10 menit) supaya tidak spam
saat gangguan berjam-jam; komponen yang aktif kembali memicu satu notifikasi
pemulihan.

Seluruh state disimpan in-memory (ADR-008): cukup untuk MVP. Batasannya,
restart proses mereset grace period dan status notifikasi, sehingga saat
gangguan masih berlangsung notifikasi bisa terkirim ulang setelah restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from polywatch.application.ports import (
    Clock,
    HealthActivityRepository,
    Notifier,
)

logger = logging.getLogger(__name__)

DEFAULT_STALE_INGEST_SEC = 120.0
DEFAULT_STALE_DISCOVER_SEC = 180.0
DEFAULT_RATE_LIMIT_SEC = 600.0
#: Grace period setelah start: tanpa data sama sekali, jangan langsung alarm.
DEFAULT_GRACE_SEC = 300.0

CycleKind = Literal["discover", "result"]

_COMPONENTS: tuple[str, ...] = ("ingest", "discover", "result")


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Status satu komponen pada saat pemeriksaan."""

    name: str
    stale: bool
    notified: bool
    last_activity: datetime | None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Hasil satu pemeriksaan kesehatan (untuk log/test)."""

    components: tuple[ComponentHealth, ...]

    @property
    def stale_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.stale)


@dataclass(slots=True)
class _State:
    """State in-memory satu komponen (bukan tabel — lihat ADR-008)."""

    last_ok: datetime | None = None
    stale: bool = False
    notified: bool = False
    last_notified_at: datetime | None = None


def _fmt(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment is not None else "belum ada"


def _stale_text(name: str, stale_sec: float, last: datetime | None) -> str:
    menit = max(int(stale_sec // 60), 1)
    label = {
        "ingest": f"tidak ada trade baru dari market LIVE lebih dari {menit} menit",
        "discover": f"siklus discover tidak berhasil lebih dari {menit} menit",
        "result": f"siklus result tidak berhasil lebih dari {menit} menit",
    }[name]
    return f"⚠️ polywatch: {label} (aktivitas terakhir: {_fmt(last)})."


def _recovery_text(name: str, last: datetime | None) -> str:
    return f"✅ polywatch: komponen {name} pulih (aktivitas terakhir: {_fmt(last)})."


class HealthMonitor:
    """Pantau kesegaran ingest & siklus scheduler, notifikasi owner (T-019)."""

    def __init__(
        self,
        *,
        activity: HealthActivityRepository,
        notifier: Notifier,
        clock: Clock,
        stale_ingest_s: float = DEFAULT_STALE_INGEST_SEC,
        stale_discover_s: float = DEFAULT_STALE_DISCOVER_SEC,
        rate_limit_s: float = DEFAULT_RATE_LIMIT_SEC,
        grace_s: float = DEFAULT_GRACE_SEC,
    ) -> None:
        self._activity = activity
        self._notifier = notifier
        self._clock = clock
        self._stale_ingest_s = max(stale_ingest_s, 0.0)
        self._stale_discover_s = max(stale_discover_s, 0.0)
        self._rate_limit_s = max(rate_limit_s, 0.0)
        self._grace_s = max(grace_s, 0.0)
        self._started_at = clock.now()
        self._state: dict[str, _State] = {name: _State() for name in _COMPONENTS}

    async def record_cycle(self, kind: CycleKind, ok: bool) -> None:
        """Catat hasil siklus scheduler; hanya sukses dihitung sebagai aktivitas.

        Siklus gagal tidak meng-update `last_ok` — penilaian stale (dan
        notifikasinya) tetap lewat `check()` berdasarkan ambang waktu.
        """
        state = self._state[kind]
        if not ok:
            return
        now = self._clock.now()
        state.last_ok = now
        was_notified = state.notified
        state.stale = False
        state.notified = False
        state.last_notified_at = None
        if was_notified:
            await self._send(_recovery_text(kind, now), "health_recovered")

    async def check(self) -> HealthReport:
        """Periksa ketiga komponen; kirim notifikasi bila perlu (rate limited)."""
        now = self._clock.now()
        last_trade = await self._activity.last_trade_at_live()
        report: list[ComponentHealth] = []
        for name in _COMPONENTS:
            if name == "ingest":
                last, stale_sec = last_trade, self._stale_ingest_s
            else:
                last, stale_sec = self._state[name].last_ok, self._stale_discover_s
            await self._evaluate(name, last, stale_sec, now)
            state = self._state[name]
            report.append(ComponentHealth(name, state.stale, state.notified, last))
        return HealthReport(tuple(report))

    async def _evaluate(
        self, name: str, last: datetime | None, stale_sec: float, now: datetime
    ) -> None:
        state = self._state[name]
        if last is None:
            if (now - self._started_at).total_seconds() < self._grace_s:
                # Grace period: aplikasi baru start tanpa data — jangan alarm.
                return
            stale = True
        else:
            stale = (now - last).total_seconds() > stale_sec

        if not stale:
            if state.notified:
                await self._send(_recovery_text(name, last), "health_recovered")
            state.stale = False
            state.notified = False
            state.last_notified_at = None
            return

        state.stale = True
        cooldown_ok = (
            state.last_notified_at is None
            or (now - state.last_notified_at).total_seconds() >= self._rate_limit_s
        )
        if cooldown_ok:
            await self._send(_stale_text(name, stale_sec, last), "health_stale")
            state.notified = True
            state.last_notified_at = now

    async def _send(self, text: str, event: str) -> None:
        """Kirim lewat port Notifier; kegagalan kirim tidak boleh melempar."""
        try:
            await self._notifier.send(text)
        except Exception:
            logger.exception("notifikasi kesehatan gagal terkirim", extra={"event": event})


__all__ = [
    "DEFAULT_GRACE_SEC",
    "DEFAULT_RATE_LIMIT_SEC",
    "DEFAULT_STALE_DISCOVER_SEC",
    "DEFAULT_STALE_INGEST_SEC",
    "ComponentHealth",
    "CycleKind",
    "HealthMonitor",
    "HealthReport",
]
