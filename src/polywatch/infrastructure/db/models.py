"""Model SQLAlchemy 2 untuk tabel MVP polywatch.

Hanya di `infrastructure/db/`. Uang & harga memakai `NUMERIC`
(Decimal Python), bukan float. Tabel v2 (`user_wallets`, `copy_rules`,
`orders`) sengaja belum dibuat.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from polywatch.domain.market import MarketStatus, Outcome, Timeframe
from polywatch.domain.trade import Action

_TIMEFRAME = Enum(Timeframe, native_enum=False, length=16)
_OUTCOME = Enum(Outcome, native_enum=False, length=16)
_MARKET_STATUS = Enum(MarketStatus, native_enum=False, length=16)
_ACTION = Enum(Action, native_enum=False, length=16)


class Base(DeclarativeBase):
    """Basis semua model polywatch."""


class AssetModel(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str]
    enabled_default: Mapped[bool] = mapped_column(Boolean, default=True)


class WatchConfigModel(Base):
    __tablename__ = "watch_config"
    __table_args__ = (UniqueConstraint("user_id", "asset_id", "timeframe", name="uq_watch_config"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    timeframe: Mapped[Timeframe] = mapped_column(_TIMEFRAME)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class MarketModel(Base):
    __tablename__ = "markets"
    __table_args__ = (Index("ix_markets_start_at", "start_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(80), unique=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    timeframe: Mapped[Timeframe] = mapped_column(_TIMEFRAME)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    token_up: Mapped[str]
    token_down: Mapped[str]
    status: Mapped[MarketStatus] = mapped_column(_MARKET_STATUS)


class MarketResultModel(Base):
    __tablename__ = "market_results"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), primary_key=True
    )
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    result: Mapped[Outcome] = mapped_column(_OUTCOME)
    volume_usdc: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    price_source: Mapped[str]
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradeModel(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", "wallet", name="uq_trades_idempotent"),
        Index("ix_trades_wallet_traded_at", "wallet", "traded_at"),
        Index("ix_trades_market_id", "market_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tx_hash: Mapped[str] = mapped_column(String(80))
    log_index: Mapped[int]
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    wallet: Mapped[str] = mapped_column(String(42))
    outcome: Mapped[Outcome] = mapped_column(_OUTCOME)
    action: Mapped[Action] = mapped_column(_ACTION)
    price: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    size: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    usdc: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PositionModel(Base):
    __tablename__ = "positions"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), primary_key=True
    )
    net_up: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    net_down: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cost_up: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cost_down: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    is_two_sided: Mapped[bool] = mapped_column(Boolean)


class WalletStatsModel(Base):
    __tablename__ = "wallet_stats"
    __table_args__ = (Index("ix_wallet_stats_score_desc", text("score DESC")),)

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    markets_count: Mapped[int]
    wins: Mapped[int]
    win_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    avg_entry: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    edge: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    roi: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    score: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    is_market_maker: Mapped[bool] = mapped_column(Boolean)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WatchlistModel(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)


class AlertSettingsModel(Base):
    __tablename__ = "alert_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    min_score: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    min_usdc: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cooldown_sec: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)


class AlertSentModel(Base):
    __tablename__ = "alerts_sent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    wallet: Mapped[str] = mapped_column(String(42))
    market_id: Mapped[int] = mapped_column(BigInteger)
    trade_id: Mapped[int | None]
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delay_ms: Mapped[int]


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
