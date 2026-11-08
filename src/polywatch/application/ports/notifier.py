"""Port notifikasi keluar (Telegram di MVP)."""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    """Pengirim pesan ke pengguna."""

    async def send(self, text: str, chat_id: int | None = None) -> None: ...
