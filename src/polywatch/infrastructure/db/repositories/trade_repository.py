"""Implementasi port TradeRepository untuk PostgreSQL (SQLAlchemy 2 async).

T-013 hanya membutuhkan pembacaan: `wallets_for_market` dan
`resolved_trades_for_wallet`. Metode tulis (`insert_many`,
`last_traded_at`) merupakan scope T-011 (`ingest_trades`) dan belum
diimplementasikan di sini — Technical debt: satukan saat T-011.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.domain.trade import Trade
from polywatch.infrastructure.db.models import MarketResultModel, TradeModel


class SqlAlchemyTradeRepository:
    """Repository trade (baca + tulis idempoten) dengan session dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        """Sisipkan batch idempoten; kembalikan jumlah baris baru.

        `ON CONFLICT (tx_hash, log_index, wallet) DO NOTHING` — menjalankan
        ingest ulang (reconnect WS, backfill, replay) tidak menduplikasi.
        Baris duplikat dalam satu batch disatukan lebih dulu ( statement
        tunggal dengan same conflict key akan error tanpa dedupe).
        """
        unique: dict[tuple[str, int, str], Trade] = {}
        for trade in trades:
            unique[(trade.tx_hash, trade.log_index, trade.wallet)] = trade
        if not unique:
            return 0
        stmt = (
            pg_insert(TradeModel)
            .values(
                [
                    {
                        "tx_hash": t.tx_hash,
                        "log_index": t.log_index,
                        "market_id": t.market_id,
                        "wallet": t.wallet,
                        "outcome": t.outcome,
                        "action": t.action,
                        "price": t.price,
                        "size": t.size,
                        "usdc": t.usdc,
                        "traded_at": t.traded_at,
                    }
                    for t in unique.values()
                ]
            )
            .on_conflict_do_nothing(constraint="uq_trades_idempotent")
            .returning(TradeModel.id)
        )
        result = await self._session.execute(stmt)
        return len(result.scalars().all())

    async def count_since(self, since: datetime) -> int:
        """Jumlah trade sejak waktu tertentu (untuk /status)."""
        stmt = select(func.count()).select_from(TradeModel).where(TradeModel.traded_at >= since)
        return int(await self._session.scalar(stmt) or 0)

    async def last_traded_at(self, market_id: int) -> datetime | None:
        """Waktu trade terakhir market (untuk backfill sejak terakhir)."""
        stmt = select(func.max(TradeModel.traded_at)).where(TradeModel.market_id == market_id)
        return await self._session.scalar(stmt)

    async def wallets_for_market(self, market_id: int) -> Sequence[str]:
        """Wallet unik yang pernah trade di market ini (urut)."""
        stmt = (
            select(TradeModel.wallet)
            .where(TradeModel.market_id == market_id)
            .distinct()
            .order_by(TradeModel.wallet)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        """Semua trade wallet pada market yang sudah resolve + arah hasilnya."""
        stmt = (
            select(TradeModel, MarketResultModel.result)
            .join(MarketResultModel, MarketResultModel.market_id == TradeModel.market_id)
            .where(TradeModel.wallet == wallet)
            .order_by(TradeModel.market_id, TradeModel.traded_at)
        )
        result = await self._session.execute(stmt)

        resolved: list[ResolvedTrade] = []
        for row in result.all():
            trade_row = row[0]
            winning = row[1]
            resolved.append(
                ResolvedTrade(
                    trade=_to_domain(trade_row),
                    winning=winning,
                    last_trade_at=trade_row.traded_at,
                )
            )
        return resolved


def _to_domain(row: TradeModel) -> Trade:
    return Trade(
        tx_hash=row.tx_hash,
        log_index=row.log_index,
        market_id=row.market_id,
        wallet=row.wallet,
        outcome=row.outcome,
        action=row.action,
        price=row.price,
        size=row.size,
        usdc=row.usdc,
        traded_at=row.traded_at,
    )
