"""Tampilan bot Telegram: handler perintah + middleware guard owner (T-014).

Handler hanya memanggil use case/repository lewat port — tidak ada SQL atau
HTTP di layer ini. Semua teks pesan Bahasa Indonesia, ringkas, tanpa emoji
berlebihan. Pesan dibatasi panjangnya (Telegram 4096 karakter) oleh
`_truncate`.

Middleware `OwnerOnlyMiddleware` menolak chat selain `owner_chat_id` dengan
satu baris generik — detail internal tidak pernah dibocorkan.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from polywatch.application.dto.settings import WatchEntry
from polywatch.application.use_cases.bot_status import BotStatus
from polywatch.application.use_cases.list_results import ListResults
from polywatch.application.use_cases.manage_settings import (
    InvalidAssetError as SettingsInvalidAssetError,
)
from polywatch.application.use_cases.manage_settings import (
    ManageSettings,
    UnknownAssetError,
)
from polywatch.domain.errors import InvalidAssetError
from polywatch.domain.market import Timeframe

logger = logging.getLogger("polywatch.bot")

#: Batas Telegram (4096) minus margin.
MAX_MESSAGE_LEN = 3800

DENIED_TEXT = "Akses ditolak."
HELP_TEXT = (
    "polywatch — pantau market Up/Down Polymarket.\n\n"
    "Perintah:\n"
    "/status — ringkasan status bot\n"
    "/markets — daftar aset yang dipantau + toggle on/off\n"
    "/results <aset> <5m|15m> [n] — hasil market terakhir"
)


def _truncate(text: str, limit: int = MAX_MESSAGE_LEN) -> str:
    """Potong pesan agar selalu di bawah batas Telegram."""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(dipotong)"


class OwnerOnlyMiddleware(BaseMiddleware):
    """Layani hanya owner; chat lain ditolak satu baris lalu dihentikan."""

    def __init__(self, owner_chat_id: int) -> None:
        self._owner_chat_id = owner_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = _chat_id(event)
        if chat_id is None or chat_id != self._owner_chat_id:
            logger.info(
                "akses ditolak",
                extra={"event": "bot_access_denied", "chat_id": chat_id},
            )
            if isinstance(event, Message):
                await event.answer(DENIED_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(DENIED_TEXT, show_alert=False)
            return None
        return await handler(event, data)


def _chat_id(event: TelegramObject) -> int | None:
    """Ambil chat id dari Message maupun CallbackQuery."""
    if isinstance(event, Message):
        return event.chat.id
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            return event.message.chat.id
        if event.from_user is not None:
            return event.from_user.id
    return None


def format_markets(entries: Sequence[WatchEntry]) -> str:
    """Formatter murni untuk /markets (dites unit tanpa Telegram)."""
    if not entries:
        return (
            "Belum ada konfigurasi watch.\n"
            "Jalankan discovery dulu agar aset terdaftar, lalu buka /markets."
        )
    lines = ["Aset yang dipantau (tombol untuk on/off):"]
    for entry in sorted(entries, key=lambda e: (e.asset, e.timeframe.value)):
        state = "aktif" if entry.enabled else "nonaktif"
        lines.append(f"- {entry.asset.upper()} {entry.timeframe.value}: {state}")
    return "\n".join(lines)


def markets_keyboard(entries: Sequence[WatchEntry]) -> InlineKeyboardMarkup:
    """Tombol inline toggle per asetxtimeframe; data = `toggle:<aset>:<tf>`."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{e.asset.upper()} {e.timeframe.value} — {'ON' if e.enabled else 'OFF'}",
                callback_data=f"toggle:{e.asset}:{e.timeframe.value}",
            )
        ]
        for e in sorted(entries, key=lambda x: (x.asset, x.timeframe.value))
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_router(
    owner_chat_id: int,
    status_use_case: BotStatus,
    settings_use_case: ManageSettings,
    results_use_case: ListResults,
) -> Router:
    """Router berisi seluruh handler perintah (tanpa middleware)."""
    router = Router(name="commands")

    @router.message(CommandStart())
    async def _start(message: Message) -> None:
        await message.answer(_truncate(HELP_TEXT))

    @router.message(Command("status"))
    async def _status(message: Message) -> None:
        await message.answer(_truncate(await status_use_case.execute()))

    @router.message(Command("markets"))
    async def _markets(message: Message) -> None:
        entries = await settings_use_case.list_watch(owner_chat_id)
        await message.answer(
            _truncate(format_markets(entries)),
            reply_markup=markets_keyboard(entries),
        )

    @router.callback_query(F.data.startswith("toggle:"))
    async def _toggle(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Permintaan tidak dikenal.", show_alert=False)
            return
        _, asset, timeframe_raw = parts
        try:
            timeframe = Timeframe(timeframe_raw)
        except ValueError:
            await callback.answer("Timeframe tidak valid.", show_alert=False)
            return
        entries = await settings_use_case.list_watch(owner_chat_id)
        current = next(
            (e.enabled for e in entries if e.asset == asset and e.timeframe == timeframe),
            None,
        )
        if current is None:
            await callback.answer("Aset tidak ditemukan.", show_alert=False)
            return
        try:
            await settings_use_case.set_watch_enabled(owner_chat_id, asset, timeframe, not current)
        except (InvalidAssetError, SettingsInvalidAssetError, UnknownAssetError, ValueError):
            await callback.answer("Gagal memperbarui konfigurasi.", show_alert=False)
            return
        new_entries = await settings_use_case.list_watch(owner_chat_id)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                _truncate(format_markets(new_entries)),
                reply_markup=markets_keyboard(new_entries),
            )
        state = "aktif" if not current else "nonaktif"
        await callback.answer(f"{asset.upper()} {timeframe.value}: {state}")

    @router.message(Command("results"))
    async def _results(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if not args:
            await message.answer("Pemakaian: /results <aset> <5m|15m> [n]")
            return
        if len(args) < 2:
            await message.answer("Aset dan timeframe wajib. Contoh: /results btc 5m 5")
            return
        asset_arg, timeframe_arg = args[0], args[1]
        limit: int | None = None
        if len(args) >= 3:
            try:
                limit = int(args[2])
            except ValueError:
                await message.answer("Jumlah baris harus angka. Contoh: /results btc 5m 5")
                return
        if limit is not None and not 1 <= limit <= 20:
            await message.answer("Jumlah baris harus 1-20.")
            return
        try:
            text = await results_use_case.execute(asset_arg, timeframe_arg, limit)
        except InvalidAssetError:
            await message.answer("Aset tidak valid. Contoh: btc, eth")
            return
        except ValueError:
            await message.answer("Timeframe harus 5m atau 15m.")
            return
        await message.answer(_truncate(text))

    return router


def build_dispatcher(
    bot: Bot,
    owner_chat_id: int,
    status_use_case: BotStatus,
    settings_use_case: ManageSettings,
    results_use_case: ListResults,
) -> Dispatcher:
    """Rakit Dispatcher: guard owner + router perintah (composition root)."""
    dp = Dispatcher()
    dp.message.middleware(OwnerOnlyMiddleware(owner_chat_id))
    dp.callback_query.middleware(OwnerOnlyMiddleware(owner_chat_id))
    dp.include_router(
        build_router(owner_chat_id, status_use_case, settings_use_case, results_use_case)
    )
    return dp


__all__ = [
    "DENIED_TEXT",
    "HELP_TEXT",
    "MAX_MESSAGE_LEN",
    "OwnerOnlyMiddleware",
    "build_dispatcher",
    "build_router",
    "format_markets",
    "markets_keyboard",
]
