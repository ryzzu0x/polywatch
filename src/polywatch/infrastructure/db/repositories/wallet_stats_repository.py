"""Implementasi port WalletStatsRepository untuk PostgreSQL (T-013).

Upsert idempoten: `wallet_stats` ON CONFLICT (wallet) — menjalankan
`recompute_scores` dua kali menghasilkan baris yang sama (nilai terakhir
menang, karena statistik dihitung ulang penuh dari sumber).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.domain.scoring import WalletStats
from polywatch.infrastructure.db.models import WalletStatsModel


class SqlAlchemyWalletStatsRepository:
    """Repository wallet_stats dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, stats: WalletStats) -> None:
        """Upsert satu baris wallet_stats idempoten (PK wallet)."""
        stmt = (
            pg_insert(WalletStatsModel)
            .values(
                wallet=stats.wallet,
                markets_count=stats.markets_count,
                wins=stats.wins,
                win_rate=stats.win_rate,
                avg_entry=stats.avg_entry,
                edge=stats.edge,
                pnl=stats.pnl,
                roi=stats.roi,
                score=stats.score,
                is_market_maker=stats.is_market_maker,
                last_trade_at=stats.last_trade_at,
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=["wallet"],
                set_={
                    "markets_count": stats.markets_count,
                    "wins": stats.wins,
                    "win_rate": stats.win_rate,
                    "avg_entry": stats.avg_entry,
                    "edge": stats.edge,
                    "pnl": stats.pnl,
                    "roi": stats.roi,
                    "score": stats.score,
                    "is_market_maker": stats.is_market_maker,
                    "last_trade_at": stats.last_trade_at,
                    "updated_at": func.now(),
                },
            )
        )
        await self._session.execute(stmt)

    async def top(self, limit: int) -> Sequence[WalletStats]:
        stmt = (
            select(WalletStatsModel)
            .where(WalletStatsModel.is_market_maker.is_(False))
            .order_by(WalletStatsModel.score.desc(), WalletStatsModel.pnl.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(row) for row in result.scalars().all()]

    async def get(self, wallet: str) -> WalletStats | None:
        stmt = select(WalletStatsModel).where(WalletStatsModel.wallet == wallet)
        row = await self._session.scalar(stmt)
        if row is None:
            return None
        return _to_domain(row)


def _to_domain(row: WalletStatsModel) -> WalletStats:
    return WalletStats(
        wallet=row.wallet,
        markets_count=row.markets_count,
        wins=row.wins,
        avg_entry=row.avg_entry,
        pnl=row.pnl,
        staked=Decimal(0),  # tidak disimpan di tabel; skor sudah final
        is_market_maker=row.is_market_maker,
        score=row.score,
        last_trade_at=row.last_trade_at,
    )
