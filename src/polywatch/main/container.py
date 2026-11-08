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
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch import __version__
from polywatch.application.ports import (
    AlertSettingsRepository as AlertSettingsRepositoryPort,
)
from polywatch.application.ports import (
    AlertsSentRepository as AlertsSentRepositoryPort,
)
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
from polywatch.application.ports import WalletStatsRepository as WalletStatsRepositoryPort
from polywatch.application.ports import WatchConfigRepository as WatchConfigRepositoryPort
from polywatch.application.ports import WatchlistRepository as WatchlistRepositoryPort
from polywatch.application.use_cases.bot_status import BotStatus
from polywatch.application.use_cases.list_results import ListResults
from polywatch.application.use_cases.manage_alerts import ManageAlerts
from polywatch.application.use_cases.manage_settings import ManageSettings
from polywatch.application.use_cases.manage_watchlist import ManageWatchlist
from polywatch.application.use_cases.wallet_views import TopWallets, WalletDetail
from polywatch.domain.trade import Trade
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
    top_wallets: TopWallets | None = field(default=None, repr=False)
    wallet_detail: WalletDetail | None = field(default=None, repr=False)
    manage_watchlist: ManageWatchlist | None = field(default=None, repr=False)
    manage_alerts: ManageAlerts | None = field(default=None, repr=False)
    ingest_hooks: list[Any] = field(default_factory=list, repr=False)
    discover_cycle: Any | None = field(default=None, repr=False)
    record_result_cycle: Any | None = field(default=None, repr=False)
    recompute_cycle: Any | None = field(default=None, repr=False)
    health_monitor: Any | None = field(default=None, repr=False)

    def setup_scheduler(self) -> None:
        """Rakit use case siklus penuh untuk scheduler (T-018 wiring).

        DiscoverMarkets (Gamma), RecordResult (Gamma + harga), dan
        RecomputeScores dipasang di sini; runner menjalankan loop periodiknya.
        Hook on_saved ingest (alert) tetap dipasang lewat `setup_ingest`.
        """
        from polywatch.application.use_cases.discover_markets import DiscoverMarkets
        from polywatch.application.use_cases.recompute_scores import RecomputeScores
        from polywatch.application.use_cases.record_result import RecordResult
        from polywatch.infrastructure.db.repositories.market_repository import (
            SqlAlchemyMarketRepository,
        )
        from polywatch.infrastructure.db.repositories.trade_repository import (
            SqlAlchemyTradeRepository,
        )
        from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
            SqlAlchemyWalletStatsRepository,
        )
        from polywatch.infrastructure.polymarket.gamma_client import GammaMarketSource
        from polywatch.infrastructure.prices.binance_price_source import BinancePriceSource
        from polywatch.infrastructure.prices.polymarket_price_source import (
            PolymarketPriceSource,
        )

        gamma = GammaMarketSource(base_url=self.settings.polymarket_gamma_url)

        async def run_discover() -> Any:
            session = self.session_factory()
            try:
                discover = DiscoverMarkets(
                    source=gamma,
                    repository=SqlAlchemyMarketRepository(session),
                    clock=self.clock,
                )
                summary = await discover.execute()
                await session.commit()
                return summary
            finally:
                await session.close()

        async def run_record_result() -> Any:
            session = self.session_factory()
            try:
                record = RecordResult(
                    source=gamma,
                    repository=SqlAlchemyMarketRepository(session),
                    primary_price=PolymarketPriceSource(),
                    fallback_price=BinancePriceSource(),
                    clock=self.clock,
                )
                summaries = await record.execute()
                await session.commit()
                return summaries
            finally:
                await session.close()

        async def run_recompute(market_id: int) -> Any:
            session = self.session_factory()
            try:
                recompute = RecomputeScores(
                    trades=SqlAlchemyTradeRepository(session),
                    wallet_stats=SqlAlchemyWalletStatsRepository(session),
                )
                summary = await recompute.execute(market_id)
                await session.commit()
                return summary
            finally:
                await session.close()

        self.discover_cycle = run_discover
        self.record_result_cycle = run_record_result
        self.recompute_cycle = run_recompute
        logger.info(
            "use case scheduler dirakit",
            extra={"event": "container_scheduler_setup", "version": __version__},
        )

    def setup_health(self) -> None:
        """Rakit HealthMonitor (T-019): bacaan aktivitas + notifier owner.

        State kesehatan in-memory (ADR-008) — tidak ada tabel baru. Notifikasi
        keluar tetap lewat port `Notifier` (TelegramNotifier), bukan aiogram
        langsung dari use case.
        """
        from polywatch.application.use_cases.health_monitor import HealthMonitor
        from polywatch.infrastructure.db.repositories.trade_repository import (
            SqlAlchemyTradeRepository,
        )
        from polywatch.infrastructure.telegram_notifier import TelegramNotifier

        activity = SqlAlchemyTradeRepository(self.session_factory())
        notifier = TelegramNotifier(
            bot_token=self.settings.bot_token.get_secret_value(),
            owner_chat_id=self.settings.owner_chat_id,
        )
        self.health_monitor = HealthMonitor(
            activity=activity,
            notifier=notifier,
            clock=self.clock,
            stale_ingest_s=self.settings.stale_ingest_s,
            stale_discover_s=self.settings.stale_discover_s,
            rate_limit_s=self.settings.health_rate_limit_s,
            grace_s=self.settings.health_grace_s,
        )
        logger.info(
            "health monitor dirakit",
            extra={"event": "container_health_setup", "version": __version__},
        )

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

    def _wallet_stats_repo(self) -> WalletStatsRepositoryPort:
        """Repository wallet_stats pada session sendiri."""
        from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
            SqlAlchemyWalletStatsRepository,
        )

        return SqlAlchemyWalletStatsRepository(self.session_factory())

    def _watchlist_repo(self) -> WatchlistRepositoryPort:
        """Repository watchlist pada session sendiri."""
        from polywatch.infrastructure.db.repositories.watchlist_repository import (
            SqlAlchemyWatchlistRepository,
        )

        return SqlAlchemyWatchlistRepository(self.session_factory())

    def _alert_settings_repo(self) -> AlertSettingsRepositoryPort:
        """Repository alert_settings pada session sendiri."""
        from polywatch.infrastructure.db.repositories.alert_repository import (
            SqlAlchemyAlertSettingsRepository,
        )

        return SqlAlchemyAlertSettingsRepository(self.session_factory())

    def _alerts_sent_repo(self) -> AlertsSentRepositoryPort:
        """Repository alerts_sent pada session sendiri."""
        from polywatch.infrastructure.db.repositories.alert_repository import (
            SqlAlchemyAlertsSentRepository,
        )

        return SqlAlchemyAlertsSentRepository(self.session_factory())

    async def setup_bot(self) -> None:
        """Rakit use case bot + dispatcher (dipanggil runner, bukan --check)."""
        from aiogram import Bot

        from polywatch.presentation.bot.handlers import build_dispatcher

        markets, trades, users, watch = self.repositories()
        owner = self.settings.owner_chat_id
        market_stats = cast(MarketStatsRepositoryPort, markets)
        trade_stats = cast(TradeStatsRepositoryPort, trades)
        wallet_stats = self._wallet_stats_repo()
        watchlist = self._watchlist_repo()
        self.bot_status = BotStatus(market_stats, trade_stats, self.clock)
        self.manage_settings = ManageSettings(users, watch)
        self.list_results = ListResults(market_stats)
        self.top_wallets = TopWallets(wallet_stats)
        self.wallet_detail = WalletDetail(wallet_stats, users, watchlist)
        self.manage_watchlist = ManageWatchlist(users, watchlist)
        alert_settings = self._alert_settings_repo()
        self.manage_alerts = ManageAlerts(users, alert_settings)
        bot = Bot(token=self.settings.bot_token.get_secret_value())
        self._bot = bot
        self._dispatcher = build_dispatcher(
            bot,
            owner_chat_id=owner,
            status_use_case=self.bot_status,
            settings_use_case=self.manage_settings,
            results_use_case=self.list_results,
            top_use_case=self.top_wallets,
            wallet_use_case=self.wallet_detail,
            watchlist_use_case=self.manage_watchlist,
            alerts_use_case=self.manage_alerts,
        )
        logger.info(
            "use case bot dirakit",
            extra={"event": "container_bot_setup", "version": __version__},
        )

    def setup_ingest(self) -> None:
        """Rakit IngestTrades dengan hook on_saved → evaluate_alert (T-017).

        Hook hanya dipasang bila method ini dipanggil; perilaku runner tanpa
        hook tidak berubah. Trade yang tersimpan langsung dievaluasi alert.
        """
        from polywatch.application.use_cases.evaluate_alert import EvaluateAlert
        from polywatch.application.use_cases.ingest_trades import IngestTrades
        from polywatch.infrastructure.db.repositories.alert_repository import (
            SqlAlchemyAlertSettingsRepository,
            SqlAlchemyAlertsSentRepository,
        )
        from polywatch.infrastructure.db.repositories.live_token_query import (
            SqlAlchemyLiveTokenQuery,
        )
        from polywatch.infrastructure.db.repositories.market_repository import (
            SqlAlchemyMarketRepository,
        )
        from polywatch.infrastructure.db.repositories.trade_repository import (
            SqlAlchemyTradeRepository,
        )
        from polywatch.infrastructure.db.repositories.user_repository import (
            SqlAlchemyUserRepository,
        )
        from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
            SqlAlchemyWalletStatsRepository,
        )
        from polywatch.infrastructure.db.repositories.watchlist_repository import (
            SqlAlchemyWatchlistRepository,
        )
        from polywatch.infrastructure.polymarket.polygon_rpc import PolygonRpcClient
        from polywatch.infrastructure.polymarket.trade_stream import (
            OrderbookWsAdapter,
            PolygonReceiptAdapter,
        )
        from polywatch.infrastructure.telegram_notifier import TelegramNotifier

        alert_session = self.session_factory()
        markets = SqlAlchemyMarketRepository(alert_session)
        users = SqlAlchemyUserRepository(alert_session)
        wallet_stats = SqlAlchemyWalletStatsRepository(alert_session)
        watchlist = SqlAlchemyWatchlistRepository(alert_session)
        alert_settings = SqlAlchemyAlertSettingsRepository(alert_session)
        alerts_sent = SqlAlchemyAlertsSentRepository(alert_session)
        notifier = TelegramNotifier(
            bot_token=self.settings.bot_token.get_secret_value(),
            owner_chat_id=self.settings.owner_chat_id,
        )
        evaluator = EvaluateAlert(
            wallet_stats=wallet_stats,
            markets=markets,
            users=users,
            watchlist=watchlist,
            settings_repo=alert_settings,
            alerts_sent=alerts_sent,
            notifier=notifier,
            clock=self.clock,
            telegram_id=self.settings.owner_chat_id,
        )

        async def on_saved(new_trades: Sequence[Trade]) -> None:
            for trade in new_trades:
                try:
                    decision = await evaluator.evaluate(trade)
                    if not decision.sent:
                        logger.debug(
                            "alert dilewati",
                            extra={
                                "event": "alert_skipped",
                                "reason": decision.reason,
                                "wallet": trade.wallet,
                            },
                        )
                except Exception:
                    # Gagal evaluasi satu trade tidak boleh menghentikan ingest.
                    logger.exception(
                        "evaluasi alert gagal",
                        extra={"event": "alert_eval_error", "wallet": trade.wallet},
                    )
            try:
                await alert_session.commit()
            except Exception:
                await alert_session.rollback()
                logger.exception("commit alert gagal", extra={"event": "alert_commit_error"})

        async def run_ingest_once() -> None:
            session = self.session_factory()
            try:
                from polywatch.domain.market import Timeframe

                live = SqlAlchemyLiveTokenQuery(session)
                tokens = await live.live_tokens(list(Timeframe))
                stream = OrderbookWsAdapter()
                rpc = PolygonRpcClient(
                    alchemy_url=os.environ.get("ALCHEMY_RPC_URL"),
                )
                ingest = IngestTrades(
                    stream=stream,
                    receipts=PolygonReceiptAdapter(rpc),
                    trades=SqlAlchemyTradeRepository(session),
                    markets=SqlAlchemyMarketRepository(session),
                    token_markets=list(tokens),
                    clock=self.clock,
                    on_saved=on_saved,
                )
                stats = await ingest.run_once()
                logger.info(
                    "hook ingest siklus selesai",
                    extra={"event": "ingest_cycle_done", "trades_saved": stats.trades_saved},
                )
                await session.commit()
            finally:
                await session.close()

        self.ingest_hooks.append(run_ingest_once)
        logger.info(
            "ingest + alert hook dirakit",
            extra={"event": "container_ingest_setup", "version": __version__},
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
