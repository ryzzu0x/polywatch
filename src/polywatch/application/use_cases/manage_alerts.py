"""Use case manage_alerts: pengaturan alert + teks /alerts (T-017).

Menampilkan pengaturan `alert_settings` (min_score, min_usdc, cooldown_sec,
enabled) dan menyimpan perubahan dari pilihan terbatas (tombol inline di
handler). Validasi: hanya nilai dari pilihan yang dikenal.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from polywatch.application.ports import AlertSettingsRepository, UserRepository

logger = logging.getLogger(__name__)

MIN_SCORE_CHOICES = (Decimal("50"), Decimal("60"), Decimal("70"), Decimal("80"))
MIN_USDC_CHOICES = (Decimal("50"), Decimal("100"), Decimal("500"))
COOLDOWN_SEC_CHOICES = (60, 300, 900)

DEFAULT_MIN_SCORE = Decimal("70")
DEFAULT_MIN_USDC = Decimal("100")
DEFAULT_COOLDOWN_SEC = 300


class InvalidAlertSettingError(ValueError):
    """Nilai pengaturan alert di luar pilihan yang dikenal."""


class ManageAlerts:
    """Baca & ubah pengaturan alert satu pengguna (telegram_id)."""

    def __init__(
        self,
        users: UserRepository,
        settings_repo: AlertSettingsRepository,
    ) -> None:
        self._users = users
        self._settings = settings_repo

    async def get(self, telegram_id: int) -> dict[str, object]:
        """Pengaturan saat ini (dengan default bila belum ada)."""
        user_id = await self._users.get_or_create_by_telegram(telegram_id)
        values = await self._settings.get(user_id)
        return {
            "min_score": _to_decimal(values.get("min_score"), DEFAULT_MIN_SCORE),
            "min_usdc": _to_decimal(values.get("min_usdc"), DEFAULT_MIN_USDC),
            "cooldown_sec": _to_int(values.get("cooldown_sec"), DEFAULT_COOLDOWN_SEC),
            "enabled": bool(values.get("enabled", True)),
        }

    async def set_min_score(self, telegram_id: int, value: Decimal) -> dict[str, object]:
        if value not in MIN_SCORE_CHOICES:
            raise InvalidAlertSettingError(
                f"skor minimal harus salah satu dari {list(MIN_SCORE_CHOICES)}"
            )
        return await self._save(telegram_id, {"min_score": value})

    async def set_min_usdc(self, telegram_id: int, value: Decimal) -> dict[str, object]:
        if value not in MIN_USDC_CHOICES:
            raise InvalidAlertSettingError(
                f"size minimal harus salah satu dari {list(MIN_USDC_CHOICES)}"
            )
        return await self._save(telegram_id, {"min_usdc": value})

    async def set_cooldown_sec(self, telegram_id: int, value: int) -> dict[str, object]:
        if value not in COOLDOWN_SEC_CHOICES:
            raise InvalidAlertSettingError(
                f"cooldown harus salah satu dari {list(COOLDOWN_SEC_CHOICES)} detik"
            )
        return await self._save(telegram_id, {"cooldown_sec": value})

    async def set_enabled(self, telegram_id: int, enabled: bool) -> dict[str, object]:
        return await self._save(telegram_id, {"enabled": enabled})

    async def _save(self, telegram_id: int, patch: dict[str, object]) -> dict[str, object]:
        user_id = await self._users.get_or_create_by_telegram(telegram_id)
        current = await self._settings.get(user_id)
        values = {
            "min_score": _to_decimal(current.get("min_score"), DEFAULT_MIN_SCORE),
            "min_usdc": _to_decimal(current.get("min_usdc"), DEFAULT_MIN_USDC),
            "cooldown_sec": _to_int(current.get("cooldown_sec"), DEFAULT_COOLDOWN_SEC),
            "enabled": bool(current.get("enabled", True)),
        }
        merged: dict[str, object] = dict(values)
        merged.update(patch)
        await self._settings.save(user_id, merged)
        logger.info(
            "alert_settings diperbarui",
            extra={"event": "alert_settings_updated", "telegram_id": telegram_id, **patch},
        )
        return values

    @staticmethod
    def format(values: dict[str, object]) -> str:
        """Formatter murni teks /alerts (dites unit)."""
        cooldown = _to_int(values["cooldown_sec"], DEFAULT_COOLDOWN_SEC)
        cooldown_text = f"{cooldown} dtk" if cooldown < 60 else f"{cooldown // 60} mnt"
        enabled = "aktif" if values["enabled"] else "nonaktif"
        return (
            "Pengaturan alert (tombol untuk mengubah):\n"
            f"Status: {enabled}\n"
            f"Skor minimal: {values['min_score']}\n"
            f"Size minimal: ${values['min_usdc']}\n"
            f"Cooldown per wallet: {cooldown_text}"
        )


def _to_decimal(raw: object, default: Decimal) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _to_int(raw: object, default: int) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


__all__ = [
    "COOLDOWN_SEC_CHOICES",
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_MIN_USDC",
    "MIN_SCORE_CHOICES",
    "MIN_USDC_CHOICES",
    "InvalidAlertSettingError",
    "ManageAlerts",
]
