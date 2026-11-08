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
from decimal import Decimal
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
from polywatch.application.use_cases.manage_alerts import (
    InvalidAlertSettingError,
    ManageAlerts,
)
from polywatch.application.use_cases.manage_settings import (
    InvalidAssetError as SettingsInvalidAssetError,
)
from polywatch.application.use_cases.manage_settings import (
    ManageSettings,
    UnknownAssetError,
)
from polywatch.application.use_cases.manage_watchlist import ManageWatchlist
from polywatch.application.use_cases.wallet_views import TopWallets, WalletDetail
from polywatch.domain.errors import InvalidAssetError, InvalidWalletError
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
    "/results <aset> <5m|15m> [n] — hasil market terakhir\n"
    "/top [n] — wallet skor tertinggi (default 10, maks 25)\n"
    "/wallet <alamat> — statistik satu wallet\n"
    "/watch <alamat> / /unwatch <alamat> — kelola watchlist wallet\n"
    "/alerts — pengaturan alert copy-trade"
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


class SessionReleaseMiddleware(BaseMiddleware):
    """Commit + remove session DB task ini setelah update selesai (T-023).

    aiogram memproses update sebagai task asyncio terpisah; use case bot
    dibuat per-update dengan session DB sendiri (provider di container).
    Middleware ini menjalankan unit-of-work per update: commit bila handler
    sukses (rollback otomatis bila exception — `remove()` menutup session
    tanpa commit), lalu mengembalikan koneksi ke pool. Jalur error pun tetap
    melepas session sehingga koneksi tidak bocor.
    """

    def __init__(self, release: Callable[[], Awaitable[None]]) -> None:
        self._release = release

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            result = await handler(event, data)
        finally:
            try:
                await self._release()
            except Exception:
                logger.exception(
                    "gagal melepas session DB update",
                    extra={"event": "session_release_error"},
                )
        return result


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


def alerts_keyboard(values: dict[str, object]) -> InlineKeyboardMarkup:
    """Tombol inline pengaturan alert; data = `alert:<field>:<value>`.

    Pilihan terbatas: skor 50/60/70/80, size $50/$100/$500, cooldown
    1/5/15 mnt, on/off. Nilai saat ini ditandai dengan tanda centang.
    """
    from polywatch.application.use_cases.manage_alerts import (
        COOLDOWN_SEC_CHOICES,
        MIN_SCORE_CHOICES,
        MIN_USDC_CHOICES,
    )

    def _mark(current: object, choice: object) -> str:
        return " (x)" if str(current) == str(choice) else ""

    score_row = [
        InlineKeyboardButton(
            text=f"skor {s}{_mark(values['min_score'], s)}",
            callback_data=f"alert:score:{s}",
        )
        for s in MIN_SCORE_CHOICES
    ]
    size_row = [
        InlineKeyboardButton(
            text=f"${u}{_mark(values['min_usdc'], u)}",
            callback_data=f"alert:usdc:{u}",
        )
        for u in MIN_USDC_CHOICES
    ]
    cooldown_row = [
        InlineKeyboardButton(
            text=f"{c // 60} mnt{_mark(values['cooldown_sec'], c)}",
            callback_data=f"alert:cooldown:{c}",
        )
        for c in COOLDOWN_SEC_CHOICES
    ]
    on_off = [
        InlineKeyboardButton(
            text=f"ON{_mark(values['enabled'], True)}",
            callback_data="alert:enabled:on",
        ),
        InlineKeyboardButton(
            text=f"OFF{_mark(values['enabled'], False)}",
            callback_data="alert:enabled:off",
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[score_row, size_row, cooldown_row, on_off])


def build_router(
    owner_chat_id: int,
    status_use_case: BotStatus | Callable[[], BotStatus],
    settings_use_case: ManageSettings | Callable[[], ManageSettings],
    results_use_case: ListResults | Callable[[], ListResults],
    top_use_case: TopWallets | Callable[[], TopWallets | None] | None = None,
    wallet_use_case: WalletDetail | Callable[[], WalletDetail | None] | None = None,
    watchlist_use_case: ManageWatchlist | Callable[[], ManageWatchlist | None] | None = None,
    alerts_use_case: ManageAlerts | Callable[[], ManageAlerts | None] | None = None,
) -> Router:
    """Router berisi seluruh handler perintah (tanpa middleware).

    Use case boleh berupa instance atau provider (`Callable[[], UseCase]`,
    T-023): provider dipanggil di awal tiap update sehingga use case (dan
    session DB-nya) dibuat per-update, tidak dipakai bersama lintas update.
    """

    def _resolve(provider_or_use_case: Any) -> Any:
        """Ambil instance use case: panggil provider bila berupa callable."""
        return provider_or_use_case() if callable(provider_or_use_case) else provider_or_use_case

    router = Router(name="commands")

    @router.message(CommandStart())
    async def _start(message: Message) -> None:
        await message.answer(_truncate(HELP_TEXT))

    @router.message(Command("status"))
    async def _status(message: Message) -> None:
        status = _resolve(status_use_case)
        await message.answer(_truncate(await status.execute()))

    @router.message(Command("markets"))
    async def _markets(message: Message) -> None:
        settings = _resolve(settings_use_case)
        entries = await settings.list_watch(owner_chat_id)
        await message.answer(
            _truncate(format_markets(entries)),
            reply_markup=markets_keyboard(entries),
        )

    @router.callback_query(F.data.startswith("toggle:"))
    async def _toggle(callback: CallbackQuery) -> None:
        settings = _resolve(settings_use_case)
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
        entries = await settings.list_watch(owner_chat_id)
        current = next(
            (e.enabled for e in entries if e.asset == asset and e.timeframe == timeframe),
            None,
        )
        if current is None:
            await callback.answer("Aset tidak ditemukan.", show_alert=False)
            return
        try:
            await settings.set_watch_enabled(owner_chat_id, asset, timeframe, not current)
        except (InvalidAssetError, SettingsInvalidAssetError, UnknownAssetError, ValueError):
            await callback.answer("Gagal memperbarui konfigurasi.", show_alert=False)
            return
        new_entries = await settings.list_watch(owner_chat_id)
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
        results = _resolve(results_use_case)
        try:
            text = await results.execute(asset_arg, timeframe_arg, limit)
        except InvalidAssetError:
            await message.answer("Aset tidak valid. Contoh: btc, eth")
            return
        except ValueError:
            await message.answer("Timeframe harus 5m atau 15m.")
            return
        await message.answer(_truncate(text))

    if top_use_case is not None:

        @router.message(Command("top"))
        async def _top(message: Message) -> None:
            args = (message.text or "").split()[1:]
            limit: int | None = None
            if args:
                try:
                    limit = int(args[0])
                except ValueError:
                    await message.answer("Jumlah baris harus angka. Contoh: /top 15")
                    return
            top = _resolve(top_use_case)
            assert top is not None
            await message.answer(_truncate(await top.execute(limit)))

    if wallet_use_case is not None:

        @router.message(Command("wallet"))
        async def _wallet(message: Message) -> None:
            args = (message.text or "").split()[1:]
            if not args:
                await message.answer("Pemakaian: /wallet <alamat>")
                return
            wallet = _resolve(wallet_use_case)
            assert wallet is not None
            try:
                text = await wallet.execute(message.chat.id, args[0])
            except InvalidWalletError:
                await message.answer(
                    "Alamat wallet tidak valid. Contoh: 0x diikuti 40 karakter heksadesimal."
                )
                return
            await message.answer(_truncate(text))

    if watchlist_use_case is not None:

        @router.message(Command("watch"))
        async def _watch(message: Message) -> None:
            args = (message.text or "").split()[1:]
            if not args:
                await message.answer("Pemakaian: /watch <alamat>")
                return
            watchlist = _resolve(watchlist_use_case)
            assert watchlist is not None
            try:
                text = await watchlist.watch(message.chat.id, args[0])
            except InvalidWalletError:
                await message.answer(
                    "Alamat wallet tidak valid. Contoh: 0x diikuti 40 karakter heksadesimal."
                )
                return
            await message.answer(_truncate(text))

        @router.message(Command("unwatch"))
        async def _unwatch(message: Message) -> None:
            args = (message.text or "").split()[1:]
            if not args:
                await message.answer("Pemakaian: /unwatch <alamat>")
                return
            watchlist = _resolve(watchlist_use_case)
            assert watchlist is not None
            try:
                text = await watchlist.unwatch(message.chat.id, args[0])
            except InvalidWalletError:
                await message.answer(
                    "Alamat wallet tidak valid. Contoh: 0x diikuti 40 karakter heksadesimal."
                )
                return
            await message.answer(_truncate(text))

    if alerts_use_case is not None:

        @router.message(Command("alerts"))
        async def _alerts(message: Message) -> None:
            alerts = _resolve(alerts_use_case)
            assert alerts is not None
            values = await alerts.get(message.chat.id)
            await message.answer(
                _truncate(ManageAlerts.format(values)),
                reply_markup=alerts_keyboard(values),
            )

        @router.callback_query(F.data.startswith("alert:"))
        async def _alert_setting(callback: CallbackQuery) -> None:
            alerts = _resolve(alerts_use_case)
            assert alerts is not None
            parts = (callback.data or "").split(":")
            if len(parts) != 3:
                await callback.answer("Permintaan tidak dikenal.", show_alert=False)
                return
            _, field, raw = parts
            try:
                if field == "score":
                    values = await alerts.set_min_score(
                        callback.message.chat.id if callback.message else owner_chat_id,
                        Decimal(raw),
                    )
                elif field == "usdc":
                    values = await alerts.set_min_usdc(
                        callback.message.chat.id if callback.message else owner_chat_id,
                        Decimal(raw),
                    )
                elif field == "cooldown":
                    values = await alerts.set_cooldown_sec(
                        callback.message.chat.id if callback.message else owner_chat_id,
                        int(raw),
                    )
                elif field == "enabled":
                    values = await alerts.set_enabled(
                        callback.message.chat.id if callback.message else owner_chat_id,
                        raw == "on",
                    )
                else:
                    await callback.answer("Pengaturan tidak dikenal.", show_alert=False)
                    return
            except (InvalidAlertSettingError, ValueError):
                await callback.answer("Nilai tidak valid.", show_alert=False)
                return
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _truncate(ManageAlerts.format(values)),
                    reply_markup=alerts_keyboard(values),
                )
            await callback.answer("Tersimpan.")

    return router


def build_dispatcher(
    bot: Bot,
    owner_chat_id: int,
    status_use_case: BotStatus | Callable[[], BotStatus],
    settings_use_case: ManageSettings | Callable[[], ManageSettings],
    results_use_case: ListResults | Callable[[], ListResults],
    top_use_case: TopWallets | Callable[[], TopWallets | None] | None = None,
    wallet_use_case: WalletDetail | Callable[[], WalletDetail | None] | None = None,
    watchlist_use_case: ManageWatchlist | Callable[[], ManageWatchlist | None] | None = None,
    alerts_use_case: ManageAlerts | Callable[[], ManageAlerts | None] | None = None,
    release_session: Callable[[], Awaitable[None]] | None = None,
) -> Dispatcher:
    """Rakit Dispatcher: guard owner + router perintah (composition root).

    `release_session` (opsional, T-023): callback yang me-remove session DB
    per-task setelah tiap update selesai — dipasang sebagai middleware
    terakhir sehingga selalu berjalan (termasuk jalur error handler).
    """
    dp = Dispatcher()
    dp.message.middleware(OwnerOnlyMiddleware(owner_chat_id))
    dp.callback_query.middleware(OwnerOnlyMiddleware(owner_chat_id))
    if release_session is not None:
        release_mw = SessionReleaseMiddleware(release_session)
        dp.message.middleware(release_mw)
        dp.callback_query.middleware(release_mw)
    dp.include_router(
        build_router(
            owner_chat_id,
            status_use_case,
            settings_use_case,
            results_use_case,
            top_use_case=top_use_case,
            wallet_use_case=wallet_use_case,
            watchlist_use_case=watchlist_use_case,
            alerts_use_case=alerts_use_case,
        )
    )
    return dp


__all__ = [
    "DENIED_TEXT",
    "HELP_TEXT",
    "MAX_MESSAGE_LEN",
    "OwnerOnlyMiddleware",
    "SessionReleaseMiddleware",
    "build_dispatcher",
    "build_router",
    "format_markets",
    "markets_keyboard",
]
