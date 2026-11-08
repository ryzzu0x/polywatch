"""Composition root: merangkai adapter ke use case.

Hanya `main/` yang boleh meng-import semua layer. Container memegang
settings, clock, engine/session DB (dibuat lazy supaya `--check`
tidak membuka koneksi), bot Telegram + dispatcher, dan background task
ingest (T-014). Use case bot (`bot_status`, `manage_settings`,
`list_results`) dirakit di sini — handler presentation hanya menerima
objek use case.

T-023: akses DB per-task. Session DB dikelola lewat
`async_scoped_session(..., scopefunc=asyncio.current_task)`: setiap
task asyncio (update Telegram, siklus scheduler, siklus ingest) mendapat
session sendiri; session di-remove di akhir tiap update/siklus (middleware
bot + `finally` tiap siklus) sehingga koneksi tidak bocor dan tidak ada
satu `AsyncSession` yang dipakai bersama lintas task (penyebab
`InvalidRequestError` konkuren pada /markets & /status).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_scoped_session,
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
    _scoped: async_scoped_session[AsyncSession] | None = field(default=None, repr=False)
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

    # ------------------------------------------------------------------ DB session

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

    @property
    def scoped_session(self) -> async_scoped_session[AsyncSession]:
        """Session per-task asyncio (T-023).

        Task yang berbeda (update Telegram, siklus scheduler, siklus ingest)
        mendapat session berbeda; task yang sama mendapat session yang sama
        sehingga satu update tetap satu unit-of-work. Pemanggil wajib
        me-remove session-nya di akhir via `release_session()`.
        """
        if self._scoped is None:
            self._scoped = async_scoped_session(
                self.session_factory, scopefunc=asyncio.current_task
            )
        return self._scoped

    async def release_session(self) -> None:
        """Commit unit-of-work task ini lalu remove session-nya (T-023).

        Dipanggil di `finally` middleware bot (tiap update) dan di akhir
        tiap siklus scheduler/ingest. Commit menyimpan tulisan handler
        (unit-of-work per update); `remove()` menutup session dan
        mengembalikan koneksi ke pool — aman walau task tidak pernah
        memakai session (commit pada session tanpa transaksi adalah no-op).
        """
        if self._scoped is None:
            return
        session = self._scoped()
        try:
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("commit session update gagal", extra={"event": "session_commit_error"})
        finally:
            await self._scoped.remove()

    async def _session_commit(self) -> None:
        """Commit unit-of-work task ini + log bila checkout/commit gagal."""
        session = self.scoped_session
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("commit session gagal", extra={"event": "session_commit_error"})
            raise

    # ------------------------------------------------------------------ scheduler

    def setup_scheduler(self) -> None:
        """Rakit use case siklus penuh untuk scheduler (T-018 wiring).

        DiscoverMarkets (Gamma), RecordResult (Gamma + harga), dan
        RecomputeScores dipasang di sini; runner menjalankan loop periodiknya.
        Hook on_saved ingest (alert) tetap dipasang lewat `setup_ingest`.
        T-023: tiap siklus memakai session per-task dan me-remove-nya di
        `finally` — dua loop scheduler tidak berbagi session.
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
            try:
                discover = DiscoverMarkets(
                    source=gamma,
                    repository=SqlAlchemyMarketRepository(self.scoped_session()),
                    clock=self.clock,
                    known_assets=SqlAlchemyMarketRepository(self.scoped_session()),
                    default_assets=self.settings.market_assets,
                    supplement_source=gamma,
                )
                summary = await discover.execute()
                await self._session_commit()
                return summary
            finally:
                await self.release_session()

        async def run_record_result() -> Any:
            try:
                record = RecordResult(
                    source=gamma,
                    repository=SqlAlchemyMarketRepository(self.scoped_session()),
                    primary_price=PolymarketPriceSource(),
                    fallback_price=BinancePriceSource(),
                    clock=self.clock,
                )
                summaries = await record.execute()
                await self._session_commit()
                return summaries
            finally:
                await self.release_session()

        async def run_recompute(market_id: int) -> Any:
            try:
                recompute = RecomputeScores(
                    trades=SqlAlchemyTradeRepository(self.scoped_session()),
                    wallet_stats=SqlAlchemyWalletStatsRepository(self.scoped_session()),
                )
                summary = await recompute.execute(market_id)
                await self._session_commit()
                return summary
            finally:
                await self.release_session()

        self.discover_cycle = run_discover
        self.record_result_cycle = run_record_result
        self.recompute_cycle = run_recompute
        logger.info(
            "use case scheduler dirakit",
            extra={"event": "container_scheduler_setup", "version": __version__},
        )

    # ------------------------------------------------------------------ health

    def setup_health(self) -> None:
        """Rakit HealthMonitor (T-019): bacaan aktivitas + notifier owner.

        State kesehatan in-memory (ADR-008) — tidak ada tabel baru. Notifikasi
        keluar tetap lewat port `Notifier` (TelegramNotifier), bukan aiogram
        langsung dari use case.

        T-023: repository aktivitas dibuat per-cek (session per-task) —
        tidak ada session bersama dengan handler bot.
        """
        from polywatch.application.use_cases.health_monitor import HealthMonitor
        from polywatch.infrastructure.db.repositories.trade_repository import (
            SqlAlchemyTradeRepository,
        )
        from polywatch.infrastructure.telegram_notifier import TelegramNotifier

        def activity_repo() -> Any:
            return SqlAlchemyTradeRepository(self.scoped_session())

        class _ScopedActivity:
            """Adapter kecil: tiap cek ambil session per-task lalu remove."""

            async def last_trade_at_live(inner_self) -> Any:
                try:
                    repo = activity_repo()
                    return await repo.last_trade_at_live()
                finally:
                    await self.release_session()

        notifier = TelegramNotifier(
            bot_token=self.settings.bot_token.get_secret_value(),
            owner_chat_id=self.settings.owner_chat_id,
        )
        self.health_monitor = HealthMonitor(
            activity=_ScopedActivity(),
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

    # ------------------------------------------------------------------ repos

    def repositories(
        self,
    ) -> tuple[
        MarketRepositoryPort,
        TradeRepositoryPort,
        UserRepositoryPort,
        WatchConfigRepositoryPort,
    ]:
        """Rakit repository untuk session task saat ini (T-023: scoped).

        Import terlambat: hindari ketergantungan main→infrastructure pada
        level modul... tetap legal karena main adalah composition root.
        """
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

        session = self.scoped_session()
        return (
            SqlAlchemyMarketRepository(session),
            SqlAlchemyTradeRepository(session),
            SqlAlchemyUserRepository(session),
            SqlAlchemyWatchConfigRepository(session),
        )

    def _wallet_stats_repo(self) -> WalletStatsRepositoryPort:
        """Repository wallet_stats pada session task saat ini."""
        from polywatch.infrastructure.db.repositories.wallet_stats_repository import (
            SqlAlchemyWalletStatsRepository,
        )

        return SqlAlchemyWalletStatsRepository(self.scoped_session())

    def _watchlist_repo(self) -> WatchlistRepositoryPort:
        """Repository watchlist pada session task saat ini."""
        from polywatch.infrastructure.db.repositories.watchlist_repository import (
            SqlAlchemyWatchlistRepository,
        )

        return SqlAlchemyWatchlistRepository(self.scoped_session())

    def _alert_settings_repo(self) -> AlertSettingsRepositoryPort:
        """Repository alert_settings pada session task saat ini."""
        from polywatch.infrastructure.db.repositories.alert_repository import (
            SqlAlchemyAlertSettingsRepository,
        )

        return SqlAlchemyAlertSettingsRepository(self.scoped_session())

    def _alerts_sent_repo(self) -> AlertsSentRepositoryPort:
        """Repository alerts_sent pada session task saat ini."""
        from polywatch.infrastructure.db.repositories.alert_repository import (
            SqlAlchemyAlertsSentRepository,
        )

        return SqlAlchemyAlertsSentRepository(self.scoped_session())

    # ------------------------------------------------------------------ bot

    def _bot_providers(self) -> dict[str, Callable[[], Any]]:
        """Provider use case bot per-update (T-023).

        Setiap provider merakit use case dengan repository pada session task
        yang memanggil (update Telegram berjalan sebagai task asyncio
        terpisah). Ini yang dipakai `build_dispatcher`; container tetap
        composition root dan use case tetap tidak tahu SQLAlchemy.
        """

        def status() -> BotStatus:
            markets, trades, _users, _watch = self.repositories()
            return BotStatus(
                cast(MarketStatsRepositoryPort, markets),
                cast(TradeStatsRepositoryPort, trades),
                self.clock,
            )

        def settings() -> ManageSettings:
            _markets, _trades, users, watch = self.repositories()
            return ManageSettings(users, watch)

        def results() -> ListResults:
            markets, _trades, _users, _watch = self.repositories()
            return ListResults(cast(MarketStatsRepositoryPort, markets))

        def top() -> TopWallets:
            return TopWallets(self._wallet_stats_repo())

        def wallet() -> WalletDetail:
            _markets, _trades, users, _watch = self.repositories()
            return WalletDetail(self._wallet_stats_repo(), users, self._watchlist_repo())

        def watchlist() -> ManageWatchlist:
            _markets, _trades, users, _watch = self.repositories()
            return ManageWatchlist(users, self._watchlist_repo())

        def alerts() -> ManageAlerts:
            _markets, _trades, users, _watch = self.repositories()
            return ManageAlerts(users, self._alert_settings_repo())

        return {
            "status": status,
            "settings": settings,
            "results": results,
            "top": top,
            "wallet": wallet,
            "watchlist": watchlist,
            "alerts": alerts,
        }

    async def setup_bot(self) -> None:
        """Rakit use case bot + dispatcher (dipanggil runner, bukan --check).

        T-023: use case BOT dirakit ulang per update lewat provider
        (`_bot_providers`) — setiap update Telegram mendapat use case dengan
        session DB sendiri. Session task rakitan ini di-remove setelah
        dispatcher selesai dibuat.
        """
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
        providers = self._bot_providers()
        self._dispatcher = build_dispatcher(
            bot,
            owner_chat_id=owner,
            status_use_case=providers["status"],
            settings_use_case=providers["settings"],
            results_use_case=providers["results"],
            top_use_case=providers["top"],
            wallet_use_case=providers["wallet"],
            watchlist_use_case=providers["watchlist"],
            alerts_use_case=providers["alerts"],
            release_session=self.release_session,
        )
        await self.release_session()
        logger.info(
            "use case bot dirakit",
            extra={"event": "container_bot_setup", "version": __version__},
        )

    # ------------------------------------------------------------------ ingest

    def setup_ingest(self) -> None:
        """Rakit IngestTrades dengan hook on_saved → evaluate_alert (T-017).

        Hook hanya dipasang bila method ini dipanggil; perilaku runner tanpa
        hook tidak berubah. Trade yang tersimpan langsung dievaluasi alert.

        T-023: evaluator alert dibuat per-panggilan `on_saved` dengan session
        task saat ini (ingest dan evaluasi boleh berbagi unit-of-work satu
        siklus, tapi tidak berbagi session dengan task lain).
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

        def build_evaluator() -> EvaluateAlert:
            """Evaluator alert dengan session task saat ini (per-call)."""
            session = self.scoped_session()
            return EvaluateAlert(
                wallet_stats=SqlAlchemyWalletStatsRepository(session),
                markets=SqlAlchemyMarketRepository(session),
                users=SqlAlchemyUserRepository(session),
                watchlist=SqlAlchemyWatchlistRepository(session),
                settings_repo=SqlAlchemyAlertSettingsRepository(session),
                alerts_sent=SqlAlchemyAlertsSentRepository(session),
                notifier=TelegramNotifier(
                    bot_token=self.settings.bot_token.get_secret_value(),
                    owner_chat_id=self.settings.owner_chat_id,
                ),
                clock=self.clock,
                telegram_id=self.settings.owner_chat_id,
            )

        async def on_saved(new_trades: Sequence[Trade]) -> None:
            evaluator = build_evaluator()
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
                await self._session_commit()
            except Exception:
                logger.exception("commit alert gagal", extra={"event": "alert_commit_error"})

        async def run_ingest_once() -> None:
            try:
                from polywatch.domain.market import Timeframe

                session = self.scoped_session()
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
                await self._session_commit()
            finally:
                await self.release_session()

        self.ingest_hooks.append(run_ingest_once)
        logger.info(
            "ingest + alert hook dirakit",
            extra={"event": "container_ingest_setup", "version": __version__},
        )

    # ------------------------------------------------------------------ shutdown

    async def aclose(self) -> None:
        """Tutup engine dan session bot bila terbuka (shutdown rapi)."""
        if self._bot is not None:
            from aiogram import Bot

            assert isinstance(self._bot, Bot)
            await self._bot.session.close()
            self._bot = None
            self._dispatcher = None
        if self._engine is not None:
            if self._scoped is not None:
                await self._scoped.close()
                self._scoped = None
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


def build_container(settings: Settings) -> Container:
    """Fungsi kecil untuk dipakai entrypoint/test."""
    return Container.build(settings)


def session_kwargs(container: Container) -> dict[str, Any]:  # pragma: no cover
    """Kemudahan test: kwargs pembuat AsyncSession."""
    return {"bind": container.engine, "expire_on_commit": False}
