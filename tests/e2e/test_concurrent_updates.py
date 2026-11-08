"""E2E test regresi konkurensi session DB (T-023).

Reproduksi bug produksi: `InvalidRequestError: This session is provisioning
a new connection; concurrent operations are not permitted` saat `/markets`
dan `/status` diproses bersamaan — karena container merakit use case bot
dengan SATU `AsyncSession` yang dipakai bersama lintas update Telegram.

Test menjalankan ≥5 update secara konkuren (`asyncio.gather` atas
`Dispatcher.feed_update`: /markets, /status, /top, /alerts, dan callback
toggle /markets) dengan DB nyata (`TEST_DATABASE_URL`, polywatch_test).
Semua harus sukses tanpa `InvalidRequestError`, dan tulisan dari callback
toggle harus ter-commit (unit-of-work per update).

Auto-skip bila `TEST_DATABASE_URL` tidak di-set.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from aiogram import Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.domain.market import MarketStatus, Timeframe
from polywatch.infrastructure.db.models import (
    AssetModel,
    MarketModel,
    UserModel,
    WalletStatsModel,
    WatchConfigModel,
    WatchlistModel,
)
from polywatch.main.config import Settings
from polywatch.main.container import Container

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DSN is None,
    reason="TEST_DATABASE_URL tidak di-set; e2e test DB di-skip",
)

OWNER = 42999


class _FakeBot:
    """Bot palsu: cukup untuk feed_update (context) + call Telegram no-op."""

    id = 42
    username = "polywatch_test_bot"

    async def __call__(self, method: object, request_timeout: object = None) -> object:
        return True


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DSN or "")
    from polywatch.infrastructure.db.models import Base

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def seeded(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Seed schema + data dasar (user, aset, watch_config, market, wallet)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(UserModel(telegram_id=OWNER))
        session.add_all(
            [
                AssetModel(symbol="btc", name="Bitcoin", enabled_default=True),
                AssetModel(symbol="eth", name="Ethereum", enabled_default=True),
            ]
        )
        await session.flush()
        assets = {
            a.symbol: a.id for a in (await session.execute(select(AssetModel))).scalars().all()
        }
        session.add_all(
            [
                WatchConfigModel(
                    user_id=1, asset_id=assets["btc"], timeframe=Timeframe("5m"), enabled=True
                ),
                WatchConfigModel(
                    user_id=1,
                    asset_id=assets["eth"],
                    timeframe=Timeframe("15m"),
                    enabled=False,
                ),
            ]
        )
        now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        session.add_all(
            [
                MarketModel(
                    condition_id="c-live",
                    slug="btc-updown-5m-1794110400",
                    asset_id=assets["btc"],
                    timeframe=Timeframe("5m"),
                    start_at=now,
                    end_at=now + timedelta(minutes=5),
                    token_up="tok-up",
                    token_down="tok-down",
                    status=MarketStatus.LIVE,
                ),
                MarketModel(
                    condition_id="c-upcoming",
                    slug="eth-updown-15m-1794110400",
                    asset_id=assets["eth"],
                    timeframe=Timeframe("15m"),
                    start_at=now + timedelta(minutes=10),
                    end_at=now + timedelta(minutes=25),
                    token_up="tok-up2",
                    token_down="tok-down2",
                    status=MarketStatus.UPCOMING,
                ),
            ]
        )
        session.add(
            WalletStatsModel(
                wallet="0x" + "ab" * 20,
                markets_count=5,
                wins=4,
                win_rate=Decimal("0.8000"),
                avg_entry=Decimal("0.5000"),
                edge=Decimal("0.1200"),
                pnl=Decimal("250.000000"),
                roi=Decimal("0.3200"),
                score=Decimal("77.0000"),
                is_market_maker=False,
                last_trade_at=now,
            )
        )
        session.add(WatchlistModel(user_id=1, wallet="0x" + "ab" * 20))
        await session.commit()
        yield session


@pytest_asyncio.fixture
async def container(seeded: AsyncSession) -> AsyncIterator[Container]:
    """Container asli dengan DB test — use case dirakit seperti produksi."""
    settings = Settings.model_validate(
        {
            "BOT_TOKEN": "123456:test-token-ABCDEFGHIJKLM",
            "OWNER_CHAT_ID": str(OWNER),
            "DATABASE_URL": TEST_DSN or "",
            "LOG_LEVEL": "INFO",
        }
    )
    c = Container.build(settings)
    await c.setup_bot()
    yield c
    await c.aclose()


def _msg_update(chat_id: int, text: str) -> Update:
    message = Message.model_construct(
        message_id=1,
        date=datetime(2026, 11, 8, tzinfo=UTC),
        chat=Chat.model_construct(id=chat_id, type="private"),
        text=text,
        from_user=User.model_construct(id=chat_id, is_bot=False, first_name="o"),
    )
    return Update.model_construct(update_id=1, message=message)


def _toggle_update(chat_id: int, data: str = "toggle:btc:5m") -> Update:
    message = Message.model_construct(
        message_id=2,
        date=datetime(2026, 11, 8, tzinfo=UTC),
        chat=Chat.model_construct(id=chat_id, type="private"),
        reply_markup=InlineKeyboardMarkup.model_construct(inline_keyboard=[]),
    )
    callback = CallbackQuery.model_construct(
        id="1",
        from_user=User.model_construct(id=chat_id, is_bot=False, first_name="o"),
        chat_instance="x",
        data=data,
        message=message,
    )
    return Update.model_construct(update_id=2, callback_query=callback)


class _Spies:
    """Mata-mata class-level agar aiogram tidak menembak jaringan."""

    def __init__(self) -> None:
        self.answered: list[str] = []
        self.edited: list[str] = []
        self._orig: dict[tuple[type, str], Any] = {}

    def __enter__(self) -> _Spies:
        spies = self

        async def answer(self: object, text: str = "", **kwargs: object) -> object:
            spies.answered.append(text)
            return True

        async def edit(self: object, text: str = "", **kwargs: object) -> object:
            spies.edited.append(text)
            return True

        self._orig[(Message, "answer")] = Message.answer
        self._orig[(CallbackQuery, "answer")] = CallbackQuery.answer
        self._orig[(Message, "edit_text")] = Message.edit_text
        Message.answer = answer  # type: ignore[assignment,method-assign]
        CallbackQuery.answer = answer  # type: ignore[assignment,method-assign]
        Message.edit_text = edit  # type: ignore[assignment,method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        for (cls, name), method in self._orig.items():
            setattr(cls, name, method)


async def test_update_konkuren_tanpa_invalid_request_error(
    container: Container, seeded: AsyncSession
) -> None:
    """≥5 update konkuren (gather atas feed_update) — semuanya sukses."""
    dispatcher = container._dispatcher
    assert isinstance(dispatcher, Dispatcher)

    updates = [
        _msg_update(OWNER, "/status"),
        _msg_update(OWNER, "/markets"),
        _msg_update(OWNER, "/markets"),
        _msg_update(OWNER, "/top"),
        _msg_update(OWNER, "/alerts"),
        _toggle_update(OWNER, "toggle:btc:5m"),
    ]
    with _Spies() as spies:
        # gather: setiap feed_update berjalan sebagai task terpisah —
        # dengan session bersama (bug lama) ini melempar InvalidRequestError.
        await asyncio.gather(*(dispatcher.feed_update(cast(Any, _FakeBot()), u) for u in updates))

    # semua perintah menjawab (tidak ada handler mati karena exception DB)
    assert len(spies.answered) >= 5
    assert any("BTC 5m" in t for t in spies.answered)

    # tulisan callback toggle ter-commit (unit-of-work per update)
    row = (
        await seeded.execute(
            select(WatchConfigModel.enabled).where(
                WatchConfigModel.user_id == 1,
                WatchConfigModel.asset_id
                == select(AssetModel.id).where(AssetModel.symbol == "btc").scalar_subquery(),
                WatchConfigModel.timeframe == Timeframe("5m"),
            )
        )
    ).scalar_one_or_none()
    assert row is False
