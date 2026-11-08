"""Query token market live untuk ingest T-011.

Join `markets` (status LIVE) ↔ `assets` ↔ `watch_config` (enabled):
hasil hanya market Up/Down live pada pasangan assetxtimeframe yang
di-watch aktif oleh **semua** pemanggilan watch_config (MVP single-owner:
`enabled=true` di baris konfigurasi user).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.domain.market import MarketStatus, Timeframe
from polywatch.infrastructure.db.models import (
    AssetModel,
    MarketModel,
    WatchConfigModel,
)


class SqlAlchemyLiveTokenQuery:
    """Daftar `TokenMarketInfo` market LIVE yang enabled di watch_config."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        """Market LIVE + token Up/Down + condition id, urut end_at."""
        stmt = (
            select(MarketModel)
            .join(AssetModel, AssetModel.id == MarketModel.asset_id)
            .join(WatchConfigModel, WatchConfigModel.asset_id == AssetModel.id)
            .where(
                MarketModel.status == MarketStatus.LIVE,
                WatchConfigModel.enabled.is_(True),
            )
            .order_by(MarketModel.end_at)
        )
        if timeframes:
            stmt = stmt.where(MarketModel.timeframe.in_(list(timeframes)))
        rows = (await self._session.scalars(stmt)).all()
        return [
            TokenMarketInfo(
                market_id=row.id,
                condition_id=row.condition_id,
                token_up=row.token_up,
                token_down=row.token_down,
                slug=row.slug,
            )
            for row in rows
        ]
