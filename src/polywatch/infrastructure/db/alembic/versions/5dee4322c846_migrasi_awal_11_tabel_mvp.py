"""migrasi awal 11 tabel MVP

Revision ID: 5dee4322c846
Revis: None
Create Date: 2026-11-08 11:21:23.520786

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5dee4322c846"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled_default", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_table(
        "markets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("condition_id", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column(
            "timeframe",
            sa.Enum(
                "FIVE_MINUTES",
                "FIFTEEN_MINUTES",
                name="timeframe",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_up", sa.String(), nullable=False),
        sa.Column("token_down", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "UPCOMING",
                "LIVE",
                "CLOSED",
                "RESOLVED",
                name="marketstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("condition_id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_markets_start_at", "markets", ["start_at"], unique=False)
    op.create_table(
        "watch_config",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column(
            "timeframe",
            sa.Enum(
                "FIVE_MINUTES",
                "FIFTEEN_MINUTES",
                name="timeframe",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "asset_id", "timeframe", name="uq_watch_config"),
    )
    op.create_table(
        "market_results",
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        # T-030/ADR-017: nullable — hasil resmi tetap tersimpan meski semua
        # sumber harga gagal (price_source='unavailable', harga NULL).
        sa.Column("open_price", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("close_price", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column(
            "result",
            sa.Enum("UP", "DOWN", name="outcome", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("volume_usdc", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("price_source", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_id"),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("tx_hash", sa.String(length=80), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum("UP", "DOWN", name="outcome", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Enum("BUY", "SELL", name="action", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("size", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("usdc", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("traded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tx_hash", "log_index", "wallet", name="uq_trades_idempotent"),
    )
    op.create_index("ix_trades_market_id", "trades", ["market_id"], unique=False)
    op.create_index("ix_trades_wallet_traded_at", "trades", ["wallet", "traded_at"], unique=False)
    op.create_table(
        "positions",
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("net_up", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("net_down", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cost_up", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cost_down", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("pnl", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("is_two_sided", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("wallet", "market_id"),
    )
    op.create_table(
        "wallet_stats",
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column("markets_count", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("avg_entry", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("edge", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("pnl", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("roi", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("score", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("is_market_maker", sa.Boolean(), nullable=False),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("wallet"),
    )
    op.create_index(
        "ix_wallet_stats_score_desc",
        "wallet_stats",
        [sa.literal_column("score DESC")],
        unique=False,
    )
    op.create_table(
        "watchlist",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "wallet"),
    )
    op.create_table(
        "alert_settings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("min_score", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("min_usdc", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("cooldown_sec", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "alerts_sent",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delay_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("alerts_sent")
    op.drop_table("alert_settings")
    op.drop_table("watchlist")
    op.drop_index("ix_wallet_stats_score_desc", table_name="wallet_stats")
    op.drop_table("wallet_stats")
    op.drop_table("positions")
    op.drop_index("ix_trades_wallet_traded_at", table_name="trades")
    op.drop_index("ix_trades_market_id", table_name="trades")
    op.drop_table("trades")
    op.drop_table("market_results")
    op.drop_table("watch_config")
    op.drop_index("ix_markets_start_at", table_name="markets")
    op.drop_table("markets")
    op.drop_table("users")
    op.drop_table("assets")
