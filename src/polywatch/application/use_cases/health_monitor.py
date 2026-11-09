"""Use case health_monitor: deteksi aktivitas basi + notifikasi owner (T-019, T-027).

Kesegaran tiga komponen diperiksa setiap `check()` (dipanggil dari runner,
default tiap `HEALTH_CHECK_INTERVAL_S` = 30 dtk):

- `ingest` — **heartbeat ingest** (ADR-014): `record_cycle("ingest", ok)`
  dipanggil oleh `IngestTrades` setiap kali ia benar-benar bekerja (batch
  commit tersimpan / siklus WS aktif), BUKAN trade market LIVE — jendela
  5m baru atau pasar sepi tidak lagi memicu alarm palsu. Ambang default
  `STALE_INGEST_S` = 600 dtk.
- `discover` / `result` — siklus scheduler terakhir yang berhasil, dilaporkan
  lewat `record_cycle` dari loop scheduler.

T-027 — alarm ingest hanya relevan bila ada market LIVE (`live_market_count`
dari port `HealthActivityRepository`): tanpa market live, tidak ada alarm.
`last_trade_at_live` tetap dibaca sebagai informasi tambahan di pesan alarm
dan `/status`, bukan pemicu.

Komponen basi memicu satu notifikasi owner via port `Notifier`, maksimal
satu per jenis per `rate_limit_s` (default 600 dtk / 10 menit) supaya
tidak spam saat gangguan berjam-jam; komponen yang aktif kembali memicu
satu notifikasi pemulihan.

Grace period setelah start (default 300 dtk): tanpa data sama sekali,
jangan langsung alarm — berlaku juga untuk heartbeat ingest.

T-027 — idempoten antar-restart: state terakhir notifikasi disimpan ke
store persisten (`NotificationStore`, file JSON default — ADR-014) supaya
restart berulang tidak mengirim ulang alarm yang sama.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from polywatch.application.ports import (
    Clock,
    HealthActivityRepository,
    Notifier,
)

logger = logging.getLogger(__name__)

DEFAULT_STALE_INGEST_SEC = 600.0
DEFAULT_STALE_DISCOVER_SEC = 300.0
DEFAULT_STALE_RESULT_SEC = 300.0
DEFAULT_RATE_LIMIT_SEC = 600.0
#: Grace period setelah start: tanpa data sama sekali, jangan langsung alarm.
DEFAULT_GRACE_SEC = 300.0
#: T-029/ADR-016: ambang adaptif = max(3x durasi siklus terukur, floor).
ADAPTIVE_STALE_MULTIPLIER = 3.0

CycleKind = Literal["ingest", "discover", "result"]

_COMPONENTS: tuple[str, ...] = ("ingest", "discover", "result")


class NotificationStore(Protocol):
    """Port state notifikasi persisten antar-restart (ADR-014)."""

    def load(self) -> dict[str, tuple[datetime | None, str | None]]: ...

    def save(self, records: dict[str, tuple[datetime | None, str | None]]) -> None: ...


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Status satu komponen pada saat pemeriksaan."""

    name: str
    stale: bool
    notified: bool
    last_activity: datetime | None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Hasil satu pemeriksaan kesehatan (untuk log/test).

    T-030/BUG-014: `stale_names` berisi hanya komponen yang BARU menjadi
    stale atau yang cooldown log-nya (10 menit) sudah lewat — dipakai
    runner untuk log `health_stale_detected`. `components` tetap berisi
    status penuh semua komponen.
    """

    components: tuple[ComponentHealth, ...]
    _logged_stale: tuple[str, ...] | None = None

    @property
    def stale_names(self) -> tuple[str, ...]:
        if self._logged_stale is not None:
            return self._logged_stale
        return tuple(c.name for c in self.components if c.stale)


@dataclass(slots=True)
class _State:
    """State in-memory satu komponen (synced dengan store persisten T-027)."""

    last_ok: datetime | None = None
    stale: bool = False
    notified: bool = False
    last_notified_at: datetime | None = None
    #: T-030/BUG-014: log WARNING stale terakhir per komponen (rate-limit log).
    last_stale_log_at: datetime | None = None


def _fmt(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment is not None else "belum ada"


def _stale_text_ingest(stale_sec: float, last: datetime | None, live_markets: int | None) -> str:
    menit = max(int(stale_sec // 60), 1)
    live_txt = f", market live: {live_markets}" if live_markets is not None else ""
    return (
        f"⚠️ polywatch: tidak ada heartbeat ingest lebih dari {menit} menit "
        f"(batch terakhir: {_fmt(last)}{live_txt})."
    )


def _stale_text(name: str, stale_sec: float, last: datetime | None) -> str:
    menit = max(int(stale_sec // 60), 1)
    label = {
        "discover": f"siklus discover tidak berhasil lebih dari {menit} menit",
        "result": f"siklus result tidak berhasil lebih dari {menit} menit",
    }
    return f"⚠️ polywatch: {label[name]} (aktivitas terakhir: {_fmt(last)})."


def _recovery_text(name: str, last: datetime | None) -> str:
    return f"✅ polywatch: komponen {name} pulih (aktivitas terakhir: {_fmt(last)})."


class HealthMonitor:
    """Pantau kesegaran ingest & siklus scheduler, notifikasi owner (T-019/T-027)."""

    def __init__(
        self,
        *,
        activity: HealthActivityRepository,
        notifier: Notifier,
        clock: Clock,
        stale_ingest_s: float = DEFAULT_STALE_INGEST_SEC,
        stale_discover_s: float = DEFAULT_STALE_DISCOVER_SEC,
        stale_result_s: float = DEFAULT_STALE_RESULT_SEC,
        rate_limit_s: float = DEFAULT_RATE_LIMIT_SEC,
        grace_s: float = DEFAULT_GRACE_SEC,
        state_store: NotificationStore | None = None,
    ) -> None:
        self._activity = activity
        self._notifier = notifier
        self._clock = clock
        self._stale_ingest_s = max(stale_ingest_s, 0.0)
        # T-029/ADR-016: `stale_discover_s`/`stale_result_s` adalah FLOOR —
        # ambang efektif = max(3x durasi siklus terukur, floor). Scheduler
        # melaporkan durasi via `record_cycle(kind, ok, duration_s)`.
        self._stale_floor: dict[str, float] = {
            "discover": max(stale_discover_s, 0.0),
            "result": max(stale_result_s, 0.0),
        }
        self._last_cycle_duration_s: dict[str, float] = {}
        self._rate_limit_s = max(rate_limit_s, 0.0)
        self._grace_s = max(grace_s, 0.0)
        self._state_store = state_store
        self._started_at = clock.now()
        self._state: dict[str, _State] = {name: _State() for name in _COMPONENTS}
        self._restore_notification_state()

    def _restore_notification_state(self) -> None:
        """Muat `last_notified_at`/`notified` dari store persisten (T-027).

        Restart saat gangguan masih berlangsung tidak boleh langsung
        mengirim ulang alarm — cooldown dihitung dari waktu kirim terakhir
        yang tersimpan di disk.
        """
        if self._state_store is None:
            return
        try:
            records = self._state_store.load()
        except Exception:
            logger.exception(
                "gagal memuat state notifikasi; lanjut tanpa idempotensi restart",
                extra={"event": "health_state_load_error"},
            )
            return
        now = self._clock.now()
        for name in _COMPONENTS:
            record = records.get(f"health:{name}")
            if record is None:
                continue
            last_sent_at, state = record
            self._state[name].last_notified_at = last_sent_at
            if (
                state == "stale"
                and last_sent_at is not None
                and (now - last_sent_at).total_seconds() < self._rate_limit_s
            ):
                # masih dalam cooldown antar-restart → tandai sudah dinotifikasi
                self._state[name].notified = True
                self._state[name].stale = True

    def _persist_notification_state(self, name: str) -> None:
        """Simpan state notifikasi satu komponen ke store persisten."""
        if self._state_store is None:
            return
        try:
            records = self._state_store.load()
        except Exception:
            records = {}
        state = self._state[name]
        records[f"health:{name}"] = (
            state.last_notified_at,
            "stale" if state.notified else "ok",
        )
        try:
            self._state_store.save(records)
        except Exception:
            # save() sudah me-log kegagalan internalnya di adapter
            logger.debug("state notifikasi tidak tersimpan", extra={"component": name})

    def effective_stale_s(self, name: str) -> float:
        """Ambang basi efektif untuk satu komponen (T-029/ADR-016).

        `ingest` statis; `discover`/`result` adaptif:
        `max(3x durasi siklus terukur terakhir, floor dari Settings)`.
        Tanpa durasi terukur → floor.
        """
        if name == "ingest":
            return self._stale_ingest_s
        floor = self._stale_floor.get(name, DEFAULT_STALE_DISCOVER_SEC)
        duration = self._last_cycle_duration_s.get(name)
        if duration is None:
            return floor
        return max(ADAPTIVE_STALE_MULTIPLIER * duration, floor)

    async def record_cycle(
        self, kind: CycleKind, ok: bool, duration_s: float | None = None
    ) -> None:
        """Catat hasil siklus/heartbeat; hanya sukses dihitung sebagai aktivitas.

        `kind="ingest"` dipakai untuk heartbeat ingest (batch commit /
        siklus WS aktif, ADR-014). Keberhasilan meng-update `last_ok`;
        kegagalan tidak — penilaian stale tetap lewat `check()`.

        T-030/BUG-014: pemulihan dikirim maksimal SEKALI per episode stale
        — pemanggil memakai `ok=False` untuk kegagalan; pelaporan sukses
        ulang setelah pemulihan tidak mengirim apa pun.

        T-029: `duration_s` (detik) dipakai untuk ambang adaptif — siklus
        yang wajar lebih panjang dari floor tidak lagi memicu alarm palsu.
        """
        state = self._state[kind]
        if duration_s is not None and kind in self._stale_floor:
            self._last_cycle_duration_s[kind] = max(duration_s, 0.0)
        if not ok:
            return
        now = self._clock.now()
        was_notified = state.notified
        state.last_ok = now
        state.stale = False
        state.notified = False
        state.last_notified_at = None
        state.last_stale_log_at = None
        if was_notified:
            await self._send(_recovery_text(kind, now), "health_recovered")
        self._persist_notification_state(kind)

    async def check(self) -> HealthReport:
        """Periksa ketiga komponen; kirim notifikasi bila perlu (rate limited)."""
        now = self._clock.now()
        live_markets: int | None = None
        try:
            live_markets = await self._activity.live_market_count()
        except Exception:
            logger.exception(
                "bacaan aktivitas health gagal", extra={"event": "health_activity_error"}
            )
        report: list[ComponentHealth] = []
        logged: list[str] = []
        for name in _COMPONENTS:
            last = self._state[name].last_ok
            stale_sec = self.effective_stale_s(name)
            if name == "ingest" and live_markets == 0:
                # T-027: tanpa market LIVE tidak ada yang bisa di-ingest —
                # jangan alarm; heartbeat tetap dicatat.
                state = self._state[name]
                if state.notified:
                    # T-030: pemulihan notifikasi hanya dikirim SEKALI per
                    # episode stale (BUG-014: live_markets fluktuatif per cek
                    # tidak boleh mengirim pesan pemulihan berulang).
                    state.notified = False
                    await self._send(_recovery_text(name, last or now), "health_recovered")
                state.stale = False
                state.notified = False
                state.last_stale_log_at = None
                # last_notified_at TIDAK di-reset: market live kembali muncul
                # dalam cooldown (BUG-013/BUG-014 flapping 0↔N) tidak boleh
                # mengirim alarm baru setiap 30 dtk.
                report.append(ComponentHealth(name, False, False, last))
                continue
            await self._evaluate(name, last, stale_sec, now, live_markets)
            state = self._state[name]
            report.append(ComponentHealth(name, state.stale, state.notified, last))
            # T-030/BUG-014: log stale hanya saat transisi / cooldown lewat,
            # bukan setiap cek (sebelumnya ±2 baris/menit → 1.137 baris/9,5 jam).
            if state.stale and self._should_log_stale(name):
                self._mark_stale_logged(name)
                logged.append(name)
        return HealthReport(tuple(report), tuple(logged))

    async def _evaluate(
        self,
        name: str,
        last: datetime | None,
        stale_sec: float,
        now: datetime,
        live_markets: int | None = None,
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
            state.last_stale_log_at = None
            return

        state.stale = True
        # BUG-014/T-030: cooldown per komponen dari `last_notified_at` —
        # termasuk nilai yang dimuat ulang dari state persisten saat
        # restart (lihat _restore_notification_state), jadi rate limit
        # tetap patuh lintas restart (maks 1 notifikasi / 10 menit).
        cooldown_ok = (
            state.last_notified_at is None
            or (now - state.last_notified_at).total_seconds() >= self._rate_limit_s
        )
        if cooldown_ok:
            if name == "ingest":
                text = _stale_text_ingest(stale_sec, last, live_markets)
            else:
                text = _stale_text(name, stale_sec, last)
            await self._send(text, "health_stale")
            state.notified = True
            state.last_notified_at = now
        self._persist_notification_state(name)

    def _should_log_stale(self, name: str) -> bool:
        """Rate-limit log WARNING stale per komponen (BUG-014/T-030).

        Deteksi stale tetap dievaluasi tiap cek, tetapi log WARNING hanya
        saat transisi baru ke stale atau sekali per `rate_limit_s` (10
        menit default) supaya journald tidak banjir 2 baris/menit saat
        gangguan berjam-jam. Return True juga menandai waktu log.
        """
        state = self._state[name]
        now = self._clock.now()
        if not state.stale or state.last_stale_log_at is None:
            return True
        return (now - state.last_stale_log_at).total_seconds() >= self._rate_limit_s

    def _mark_stale_logged(self, name: str) -> None:
        self._state[name].last_stale_log_at = self._clock.now()

    async def _send(self, text: str, event: str) -> None:
        """Kirim lewat port Notifier; kegagalan kirim tidak boleh melempar."""
        try:
            await self._notifier.send(text)
        except Exception:
            logger.exception("notifikasi kesehatan gagal terkirim", extra={"event": event})


__all__ = [
    "ADAPTIVE_STALE_MULTIPLIER",
    "DEFAULT_GRACE_SEC",
    "DEFAULT_RATE_LIMIT_SEC",
    "DEFAULT_STALE_DISCOVER_SEC",
    "DEFAULT_STALE_INGEST_SEC",
    "DEFAULT_STALE_RESULT_SEC",
    "ComponentHealth",
    "CycleKind",
    "HealthMonitor",
    "HealthReport",
    "NotificationStore",
]
