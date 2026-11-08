"""Unit test middleware guard owner-only (T-014) — tanpa Telegram nyata.

`aiogram` menyediakan objek Message/CallbackQuery yang bisa dibuat manual;
`event.answer` diamankan dengan objek palsu yang merekam panggilan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from polywatch.presentation.bot.handlers import DENIED_TEXT, OwnerOnlyMiddleware

OWNER = 42


class FakeAnswer:
    """Perekam panggilan `answer` milik Message/CallbackQuery."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def _make_message(chat_id: int) -> tuple[Message, FakeAnswer]:
    answer = FakeAnswer()
    message = Message.model_construct(
        message_id=1,
        date=datetime(2026, 11, 8, tzinfo=UTC),
        chat=Chat.model_construct(id=chat_id, type="private"),
        from_user=User.model_construct(id=chat_id, is_bot=False, first_name="x"),
        text="/start",
    )
    object.__setattr__(message, "answer", answer)
    return message, answer


def _make_callback(chat_id: int) -> tuple[CallbackQuery, FakeAnswer]:
    answer = FakeAnswer()
    callback = CallbackQuery.model_construct(
        id="1",
        from_user=User.model_construct(id=chat_id, is_bot=False, first_name="x"),
        chat_instance="x",
        data="toggle:btc:5m",
        message=Message.model_construct(
            message_id=1,
            date=datetime(2026, 11, 8, tzinfo=UTC),
            chat=Chat.model_construct(id=chat_id, type="private"),
            reply_markup=InlineKeyboardMarkup.model_construct(inline_keyboard=[]),
        ),
    )
    object.__setattr__(callback, "answer", answer)
    return callback, answer


async def _handler(event: Any, data: dict[str, Any]) -> str:
    return "ok"


async def test_owner_message_dilayani() -> None:
    middleware = OwnerOnlyMiddleware(OWNER)
    message, _ = _make_message(OWNER)
    result = await middleware(_handler, message, {})
    assert result == "ok"


async def test_non_owner_message_ditolak_satu_baris() -> None:
    middleware = OwnerOnlyMiddleware(OWNER)
    message, answer = _make_message(999)
    result = await middleware(_handler, message, {})
    assert result is None
    assert len(answer.calls) == 1
    args, _kwargs = answer.calls[0]
    assert args == (DENIED_TEXT,)
    assert DENIED_TEXT == "Akses ditolak."
    # tidak membocorkan detail internal: satu baris generik saja
    assert "owner" not in DENIED_TEXT.lower()


async def test_owner_callback_dilayani() -> None:
    middleware = OwnerOnlyMiddleware(OWNER)
    callback, _ = _make_callback(OWNER)
    result = await middleware(_handler, callback, {})
    assert result == "ok"


async def test_non_owner_callback_ditolak() -> None:
    middleware = OwnerOnlyMiddleware(OWNER)
    callback, answer = _make_callback(999)
    result = await middleware(_handler, callback, {})
    assert result is None
    assert len(answer.calls) == 1
