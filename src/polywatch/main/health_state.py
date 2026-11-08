"""State notifikasi health persisten antar-restart (T-027, ADR-014).

File JSON kecil (default temp dir, path dari Settings `HEALTH_STATE_FILE`)
yang menyimpan `last_sent_at` + status per jenis notifikasi. Tujuannya:
restart berulang saat gangguan berlangsung tidak mengirim ulang alarm
(spam). Dipilih file JSON, bukan tabel DB — state operational kecil,
semantik key-value per jenis, dan tidak perlu migrasi alembic untuk MVP.

Corrupt/unreadable file diperlakukan sebagai state kosong (fail-open:
monitor in-memory tetap dipakai; alarm normal tetap jalan, hanya
idempotensi antar-restart yang hilang sementara).
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress as contextlib_suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("polywatch.health_state")

_KEY_KIND = "kind"
_KEY_LAST_SENT_AT = "last_sent_at"
_KEY_STATE = "state"


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """State terakhir satu jenis notifikasi (mis. `health_stale:ingest`)."""

    kind: str
    last_sent_at: datetime | None
    state: str | None


class JsonHealthStateStore:
    """Baca/tulis state notifikasi health ke file JSON kecil.

    Mengimplementasikan port `NotificationStore` (application layer):
    dict `kind → (last_sent_at, state)` — tuple polos supaya port tidak
    bergantung pada tipe di modul ini.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, tuple[datetime | None, str | None]]:
        """Muat seluruh state; file kosong/corrupt → dict kosong."""
        records: dict[str, tuple[datetime | None, str | None]] = {}
        if not self._path.exists():
            return records
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "state health tidak terbaca; mulai dari state kosong",
                extra={"event": "health_state_unreadable", "path": str(self._path)},
            )
            return records
        for entry in raw.get("notifications", []):
            if not isinstance(entry, dict) or _KEY_KIND not in entry:
                continue
            last_raw = entry.get(_KEY_LAST_SENT_AT)
            try:
                last = datetime.fromisoformat(str(last_raw)) if last_raw is not None else None
            except ValueError:
                last = None
            state = entry.get(_KEY_STATE)
            records[str(entry[_KEY_KIND])] = (
                last,
                str(state) if state is not None else None,
            )
        return records

    def save(self, records: dict[str, tuple[datetime | None, str | None]]) -> None:
        """Tulis seluruh state (atomic-ish: tulis tmp lalu ganti)."""
        payload = {
            "notifications": [
                {
                    _KEY_KIND: kind,
                    _KEY_LAST_SENT_AT: (
                        last.isoformat(timespec="seconds") if last is not None else None
                    ),
                    _KEY_STATE: state,
                }
                for kind, (last, state) in records.items()
            ]
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception(
                "gagal menulis state health; idempotensi antar-restart hilang sementara",
                extra={"event": "health_state_write_error", "path": str(self._path)},
            )
            with contextlib_suppress(OSError):
                tmp.unlink(missing_ok=True)


__all__ = ["JsonHealthStateStore"]
