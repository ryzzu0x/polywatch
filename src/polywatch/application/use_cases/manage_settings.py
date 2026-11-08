"""Use case manage_settings: pengaturan watch asetxtimeframe per pengguna (T-008).

MVP single-owner: baris `users` dibuat otomatis dari `telegram_id` lewat port
`UserRepository`. Validasi input: `Asset` & `Timeframe` divalidasi domain, dan
aset harus sudah terdaftar di tabel `assets` (dibuat oleh discovery T-007).
Menonaktifkan asetxtimeframe menyimpan state `enabled=false` yang dipakai
`list_watch`/`enabled_assets` — siklus ingest berikutnya (T-011) memfilter
market lewat state ini sehingga yang dinonaktifkan tidak diproses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from polywatch.application.dto.settings import WatchEntry
from polywatch.application.ports import UserRepository, WatchConfigRepository
from polywatch.domain.errors import InvalidAssetError
from polywatch.domain.market import Asset, Timeframe

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES: frozenset[Timeframe] = frozenset(Timeframe)


class UnknownAssetError(ValueError):
    """Aset belum terdaftar di tabel assets (harus lewat discovery dulu)."""


@dataclass(slots=True)
class ManageSettings:
    """Pengaturan watch_config untuk satu pengguna (telegram_id)."""

    users: UserRepository
    watch_config: WatchConfigRepository

    async def set_watch_enabled(
        self, telegram_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> WatchEntry:
        """Set status watch satu pasangan asetxtimeframe.

        Baris `users` dibuat otomatis bila belum ada (idempoten). Aset yang
        belum terdaftar ditolak — hanya aset hasil discovery yang bisa
        dipantau.
        """
        validated = Asset(asset)  # validasi domain (InvalidAssetError bila rusak)
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"timeframe tidak dikenal: {timeframe!r}")

        if not await self.watch_config.asset_exists(validated.symbol):
            raise UnknownAssetError(
                f"aset {validated.symbol!r} belum terdaftar; jalankan discovery dulu"
            )

        user_id = await self.users.get_or_create_by_telegram(telegram_id)
        await self.watch_config.set_enabled(user_id, validated.symbol, timeframe, enabled)
        logger.info(
            "watch_config diperbarui",
            extra={
                "telegram_id": telegram_id,
                "asset": validated.symbol,
                "timeframe": timeframe.value,
                "enabled": enabled,
            },
        )
        return WatchEntry(asset=validated.symbol, timeframe=timeframe, enabled=enabled)

    async def list_watch(self, telegram_id: int) -> list[WatchEntry]:
        """Daftar seluruh konfigurasi watch pengguna (enabled dan tidak)."""
        user_id = await self.users.get_id_by_telegram(telegram_id)
        if user_id is None:
            return []
        return list(await self.watch_config.list_all(user_id))

    async def enabled_assets(self, telegram_id: int) -> list[WatchEntry]:
        """Hanya entri aktif — dipakai siklus ingest untuk memfilter market.

        Inilah mekanisme "menonaktifkan asetxtf menghentikan ingest siklus
        berikutnya": market dengan asetxtimeframe yang tidak ada di daftar ini
        dilewati.
        """
        entries = await self.list_watch(telegram_id)
        return [entry for entry in entries if entry.enabled]


__all__ = ["VALID_TIMEFRAMES", "InvalidAssetError", "ManageSettings", "UnknownAssetError"]
