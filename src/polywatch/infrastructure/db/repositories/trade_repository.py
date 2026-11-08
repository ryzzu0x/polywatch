"""Implementasi port TradeRepository untuk PostgreSQL (SQLAlchemy 2 async).

T-013 hanya membutuhkan pembacaan: `wallets_for_market` dan
`resolved_trades_for_wallet`. Metode tulis (`insert_many`,
`last_traded_at`) merupakan scope T-011 (`ingest_trades`) dan belum
diimplementasikan di sini — Technical debt: satukan saat T-011.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.domain.trade import Trade
from polywatch.infrastructure.db.models import MarketResultModel, TradeModel


class SqlAlchemyTradeRepository:
    """Repository trade (bagian baca) dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        raise NotImplementedError("scope T-011 ingest_trades; belum diimplementasikan")

    async def last_traded_at(self, market_id: int) -> datetime | None:
        raise NotImplementedError("scope T-011 ingest_trades; belum diimplementasikan")

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
