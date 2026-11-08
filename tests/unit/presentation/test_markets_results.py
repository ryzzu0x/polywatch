"""Unit test formatter /markets, /results, dan callback toggle (T-015).

Pakai fake repository/use case; tidak menembak Telegram nyata. Callback
toggle diuji lewat `Dispatcher.feed_update` aiogram (memakan event nyata
secara lokal tanpa jaringan).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from aiogram import Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)

from polywatch.application.dto.market import MarketResultEntry
from polywatch.application.dto.settings import WatchEntry
from polywatch.application.ports import MarketStatsRepository
from polywatch.application.use_cases.list_results import DEFAULT_LIMIT, MAX_LIMIT, ListResults
from polywatch.domain.errors import InvalidAssetError
from polywatch.domain.market import Outcome, Timeframe
from polywatch.presentation.bot.handlers import (
    build_dispatcher,
    format_markets,
    markets_keyboard,
)

OWNER = 42

# ---------------------------------------------------------------- formatter /markets


def test_format_markets_daftar_kosong() -> None:
    text = format_markets([])
    assert "Belum ada konfigurasi watch" in text


def test_format_markets_menampilkan_status_per_aset() -> None:
    entries = [
        WatchEntry(asset="eth", timeframe=Timeframe("15m"), enabled=False),
        WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True),
    ]
    text = format_markets(entries)
    assert "BTC 5m: aktif" in text
    assert "ETH 15m: nonaktif" in text
    assert text.index("BTC 5m") < text.index("ETH 15m")


def test_markets_keyboard_callback_data_toggle() -> None:
    entries = [WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True)]
    kb = markets_keyboard(entries)
    button = kb.inline_keyboard[0][0]
    assert button.callback_data == "toggle:btc:5m"
    assert "ON" in button.text


def test_markets_keyboard_kosong_tanpa_tombol() -> None:
    assert markets_keyboard([]).inline_keyboard == []


def test_format_markets_panjang_dibatasi() -> None:
    from polywatch.presentation.bot.handlers import MAX_MESSAGE_LEN, _truncate

    banyak = [
        WatchEntry(asset=f"aa{i}", timeframe=Timeframe("5m"), enabled=True) for i in range(2000)
    ]
    assert len(_truncate(format_markets(banyak))) <= MAX_MESSAGE_LEN


# ---------------------------------------------------------------- /results


class FakeResultsRepo:
    def __init__(self, rows: list[MarketResultEntry]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, Timeframe, int]] = []

    async def latest_results(
        self, asset: str, timeframe: Timeframe, limit: int
    ) -> list[MarketResultEntry]:
        self.calls.append((asset, timeframe, limit))
        return self._rows


def _entry(
    slug: str = "btc-updown-5m-1794110400", result: Outcome = Outcome.UP
) -> MarketResultEntry:
    return MarketResultEntry(
        slug=slug,
        result=result,
        open_price=Decimal("42000.5"),
        close_price=Decimal("42100.0"),
        volume_usdc=Decimal("1234.56"),
        start_at=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
    )


async def test_results_formatter_baris_kosong() -> None:
    text = ListResults.format("btc", Timeframe("5m"), [])
    assert "Belum ada hasil untuk BTC 5m" in text


async def test_results_formatter_baris_terisi() -> None:
    rows = [_entry(), _entry(result=Outcome.DOWN)]
    text = ListResults.format("btc", Timeframe("5m"), rows)
    assert "Hasil BTC 5m" in text
    assert "Up" in text
    assert "Down" in text
    assert "42000.5" in text
    assert "42100.0" in text
    assert "1234.56" in text
    assert len(text.splitlines()) == len(rows) + 1  # header + n baris


async def test_results_validasi_aset_domain() -> None:
    use_case = ListResults(cast(MarketStatsRepository, FakeResultsRepo([])))
    with pytest.raises(InvalidAssetError):
        await use_case.execute("bt c!", "5m")


async def test_results_validasi_timeframe() -> None:
    use_case = ListResults(cast(MarketStatsRepository, FakeResultsRepo([])))
    with pytest.raises(ValueError, match=r"(?i)timeframe"):
        await use_case.execute("btc", "1h")


async def test_results_default_dan_batas_limit() -> None:
    repo = FakeResultsRepo([])
    use_case = ListResults(cast(MarketStatsRepository, repo))
    await use_case.execute("btc", "5m")
    assert repo.calls == [("btc", Timeframe("5m"), DEFAULT_LIMIT)]
    await use_case.execute("btc", "5m", 999)
    assert repo.calls[-1] == ("btc", Timeframe("5m"), MAX_LIMIT)
    await use_case.execute("btc", "15m", 3)
    assert repo.calls[-1] == ("btc", Timeframe("15m"), 3)


async def test_results_asset_dinormalisasi_lowercase() -> None:
    repo = FakeResultsRepo([])
    use_case = ListResults(cast(MarketStatsRepository, repo))
    await use_case.execute("BTC", "5m")
    assert repo.calls[0][0] == "btc"


# ---------------------------------------------------------------- callback toggle


class FakeSettings:
    """Fake ManageSettings: mencatat panggilan set_watch_enabled."""

    def __init__(self, entries: list[WatchEntry]) -> None:
        self.entries = entries
        self.set_calls: list[tuple[str, Timeframe, bool]] = []

    async def list_watch(self, telegram_id: int) -> list[WatchEntry]:
        return list(self.entries)

    async def set_watch_enabled(
        self, telegram_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> WatchEntry:
        self.set_calls.append((asset, timeframe, enabled))
        self.entries = [
            (
                WatchEntry(asset=e.asset, timeframe=e.timeframe, enabled=enabled)
                if e.asset == asset and e.timeframe == timeframe
                else e
            )
            for e in self.entries
        ]
        return WatchEntry(asset=asset, timeframe=timeframe, enabled=enabled)


class _StubUseCase:
    async def execute(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover
        return "stub"


class _FakeBot:
    """Bot palsu: cukup untuk feed_update (context/logging) dan call method
    Telegram (callback.answer) yang kita tangkap sebagai no-op."""

    id = 42
    username = "polywatch_test_bot"

    async def __call__(self, method: object, request_timeout: object = None) -> object:
        return True


def _build_test_dispatcher(fake: FakeSettings) -> Dispatcher:
    """Dispatcher uji: handler asli + use case settings palsu."""
    return build_dispatcher(
        bot=cast(Any, _FakeBot()),  # bot dipakai feed_update (context only)
        owner_chat_id=OWNER,
        status_use_case=cast(Any, _StubUseCase()),
        settings_use_case=cast(Any, fake),
        results_use_case=cast(Any, _StubUseCase()),
    )


def _toggle_update(chat_id: int, data: str = "toggle:btc:5m") -> Update:
    message = Message.model_construct(
        message_id=1,
        date=datetime(2026, 11, 8, tzinfo=UTC),
        chat=Chat.model_construct(id=chat_id, type="private"),
        reply_markup=InlineKeyboardMarkup.model_construct(inline_keyboard=[]),
    )

    edited: list[str] = []
    answered: list[str] = []

    async def _edit_text(text: str, reply_markup: object = None) -> None:
        edited.append(text)

    async def _answer(text: str, show_alert: bool = False) -> None:
        answered.append(text)

    object.__setattr__(message, "edit_text", _edit_text)
    object.__setattr__(message, "_edited_log", edited)
    object.__setattr__(message, "_answered_log", answered)
    callback = CallbackQuery.model_construct(
        id="1",
        from_user=User.model_construct(id=chat_id, is_bot=False, first_name="o"),
        chat_instance="x",
        data=data,
        message=message,
    )
    object.__setattr__(callback, "answer", _answer)
    return Update.model_construct(update_id=1, callback_query=callback)


async def test_toggle_callback_memanggil_use_case_dengan_kebalikan() -> None:
    fake = FakeSettings([WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True)])
    dp = _build_test_dispatcher(fake)

    update = _toggle_update(OWNER)
    cb = update.callback_query
    assert cb is not None
    assert cb.message is not None
    # aiogram memvalidasi ulang event (deep copy), jadi mata-mata dipasang
    # pada class method, bukan instance.
    from aiogram.types import Message as _Msg

    edited: list[str] = []
    answered: list[str] = []
    orig_edit = _Msg.edit_text
    orig_cb_answer = type(cb).answer

    async def _spy_edit(self: object, text: str, **kwargs: object) -> object:
        edited.append(text)
        return True

    async def _spy_answer(self: object, text: str = "", **kwargs: object) -> object:
        answered.append(text)
        return True

    _Msg.edit_text = _spy_edit  # type: ignore[assignment,method-assign]
    type(cb).answer = _spy_answer  # type: ignore[assignment,method-assign]
    try:
        await dp.feed_update(cast(Any, _FakeBot()), update)
    finally:
        _Msg.edit_text = orig_edit  # type: ignore[method-assign]
        type(cb).answer = orig_cb_answer  # type: ignore[method-assign]

    assert fake.set_calls == [("btc", Timeframe("5m"), False)]
    # pesan diedit ulang dengan status baru + konfirmasi singkat
    assert edited
    assert "BTC 5m: nonaktif" in edited[-1]
    assert answered
    assert "nonaktif" in answered[-1]


async def test_toggle_callback_idempoten_eksekusi_kedua() -> None:
    fake = FakeSettings([WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True)])
    dp = _build_test_dispatcher(fake)

    await dp.feed_update(cast(Any, _FakeBot()), _toggle_update(OWNER))
    await dp.feed_update(cast(Any, _FakeBot()), _toggle_update(OWNER))

    # dua kali toggle kembali ke keadaan awal (idempoten per klik)
    assert fake.set_calls == [("btc", Timeframe("5m"), False), ("btc", Timeframe("5m"), True)]


async def test_toggle_callback_aset_tidak_dikenal_tidak_memanggil_use_case() -> None:
    fake = FakeSettings([WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True)])
    dp = _build_test_dispatcher(fake)

    await dp.feed_update(cast(Any, _FakeBot()), _toggle_update(OWNER, data="toggle:xyz:15m"))

    assert fake.set_calls == []


async def test_toggle_callback_non_owner_ditolak() -> None:
    fake = FakeSettings([WatchEntry(asset="btc", timeframe=Timeframe("5m"), enabled=True)])
    dp = _build_test_dispatcher(fake)

    await dp.feed_update(cast(Any, _FakeBot()), _toggle_update(999))

    assert fake.set_calls == []
