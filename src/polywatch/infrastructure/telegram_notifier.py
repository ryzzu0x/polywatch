"""Adapter Notifier: Telegram via aiogram (implementasi port `Notifier`).

Hanya pengiriman pesan — bukan handler bot (bot ada di T-014). Chat tujuan
default `OWNER_CHAT_ID` dari settings; bisa dioverride per pesan lewat
`chat_id`. Tidak ada pengiriman nyata saat test: unit test memakai notifier
palsu, adapter ini diuji tipenya saja.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Kirim notifikasi keluar lewat Bot API Telegram."""

    def __init__(self, bot_token: str, owner_chat_id: int) -> None:
        self._bot = Bot(token=bot_token)
        self._owner_chat_id = owner_chat_id

    async def send(self, text: str, chat_id: int | None = None) -> None:
        """Kirim pesan ke owner (atau chat spesifik). Error di-log, tidak di-raise.

        Notifikasi adalah jalur sekunder — kegagalan kirim tidak boleh
        menggagalkan use case pemanggil.
        """
        target = chat_id if chat_id is not None else self._owner_chat_id
        try:
            await self._bot.send_message(chat_id=target, text=text)
        except TelegramAPIError as exc:
            logger.error(
                "notifikasi Telegram gagal terkirim",
                extra={"event": "notify_failed", "error": str(exc), "chat_id": target},
            )

    async def aclose(self) -> None:
        """Tutup session httpx milik Bot (shutdown rapi)."""
        await self._bot.session.close()
