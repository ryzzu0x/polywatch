"""Composition root: merangkai adapter ke use case.

Hanya `main/` yang boleh meng-import semua layer. Container memegang
settings, clock, engine/session DB (dibuat lazy supaya `--check`
tidak membuka koneksi), bot Telegram + dispatcher, dan background task
ingest (T-014). Use case bot (`bot_status`, `manage_settings`,
`list_results`) dirakit di sini — handler presentation hanya menerima
objek use case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch import __version__
from polywatch.application.ports import Clock
from polywatch.application.ports import MarketRepository as MarketRepositoryPort
from polywatch.application.ports import (
    MarketStatsRepository as MarketStatsRepositoryPort,
)
from polywatch.application.ports import (
    TradeRepository as TradeRepositoryPort,
)
from polywatch.application.ports import (
    TradeStatsRepository as TradeStatsRepositoryPort,
)
from polywatch.application.ports import UserRepository as UserRepositoryPort
from polywatch.application.ports import WatchConfigRepository as WatchConfigRepositoryPort
from polywatch.application.use_cases.bot_status import BotStatus
from polywatch.application.use_cases.list_results import ListResults
from polywatch.application.use_cases.manage_settings import ManageSettings
from polywatch.infrastructure.clock import SystemClock
from polywatch.main.config import Settings

logger = logging.getLogger("polywatch.container")


def create_engine(settings: Settings) -> AsyncEngine:
    """Buat engine SQLAlchemy async dari settings (asyncpg)."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )


@dataclass(slots=True)
class Container:
    """Objek yang dirakit sekali saat aplikasi start."""

    settings: Settings
    clock: Clock
    _engine: AsyncEngine | None = field(default=None, repr=False)
    _session_factory: async_sessionmaker[AsyncSession] | None = field(default=None, repr=False)
    _bot: object | None = field(default=None, repr=False)
    _dispatcher: object | None = field(default=None, repr=False)
    bot_status: BotStatus | None = field(default=None, repr=False)
    manage_settings: ManageSettings | None = field(default=None, repr=False)
    list_results: ListResults | None = field(default=None, repr=False)
    ingest_hooks: list[Any] = field(default_factory=list, repr=False)

    @classmethod
    def build(cls, settings: Settings) -> Container:
        return cls(settings=settings, clock=SystemClock())

    @property
    def engine(self) -> AsyncEngine:
        """Engine DB, dibuat saat pertama kali dipakai."""
        if self._engine is None:
            self._engine = create_engine(self.settings)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        return self._session_factory

    def repositories(
        self,
    ) -> tuple[
        MarketRepositoryPort,
        TradeRepositoryPort,
        UserRepositoryPort,
        WatchConfigRepositoryPort,
    ]:
        """Rakit repository untuk satu session DB (import terlambat: hindari
        ketergantungan main→infrastructure pada level modul... tetap legal
        karena main adalah composition root)."""
        from polywatch.infrastructure.db.repositories.market_repository import (
            SqlAlchemyMarketRepository,
        )
        from polywatch.infrastructure.db.repositories.trade_repository import (
            SqlAlchemyTradeRepository,
        )
        from polywatch.infrastructure.db.repositories.user_repository import (
            SqlAlchemyUserRepository,
        )
        from polywatch.infrastructure.db.repositories.watch_config_repository import (
            SqlAlchemyWatchConfigRepository,
        )

        session = self.session_factory()
        return (
            SqlAlchemyMarketRepository(session),
            SqlAlchemyTradeRepository(session),
            SqlAlchemyUserRepository(session),
            SqlAlchemyWatchConfigRepository(session),
        )

    async def setup_bot(self) -> None:
        """Rakit use case bot + dispatcher (dipanggil runner, bukan --check)."""
        from aiogram import Bot

        from polywatch.presentation.bot.handlers import build_dispatcher

        markets, trades, users, watch = self.repositories()
        owner = self.settings.owner_chat_id
        market_stats = cast(MarketStatsRepositoryPort, markets)
        trade_stats = cast(TradeStatsRepositoryPort, trades)
        self.bot_status = BotStatus(market_stats, trade_stats, self.clock)
        self.manage_settings = ManageSettings(users, watch)
        self.list_results = ListResults(market_stats)
        bot = Bot(token=self.settings.bot_token.get_secret_value())
        self._bot = bot
        self._dispatcher = build_dispatcher(
            bot,
            owner_chat_id=owner,
            status_use_case=self.bot_status,
            settings_use_case=self.manage_settings,
            results_use_case=self.list_results,
        )
        logger.info(
            "use case bot dirakit",
            extra={"event": "container_bot_setup", "version": __version__},
        )

    async def aclose(self) -> None:
        """Tutup engine dan session bot bila terbuka (shutdown rapi)."""
        if self._bot is not None:
            from aiogram import Bot

            assert isinstance(self._bot, Bot)
            await self._bot.session.close()
            self._bot = None
            self._dispatcher = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


def build_container(settings: Settings) -> Container:
    """Fungsi kecil untuk dipakai entrypoint/test."""
    return Container.build(settings)


def session_kwargs(container: Container) -> dict[str, Any]:  # pragma: no cover
    """Kemudahan test: kwargs pembuat AsyncSession."""
    return {"bind": container.engine, "expire_on_commit": False}
