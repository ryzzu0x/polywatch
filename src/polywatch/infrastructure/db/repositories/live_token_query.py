"""Query token market live untuk ingest T-011/T-026.

Semantik watch default-enabled (ADR-012): market LIVE dikembalikan untuk
**semua** pasangan asetxtimeframe, KECUALI ada baris `watch_config`
override `enabled=false` (dari salah satu pemilik konfigurasi — MVP
single-owner) untuk pasangan itu. Tabel `watch_config` kosong berarti
semua aset x timeframe aktif — "belum ada pengaturan" bukan "semua
dimatikan".

Join `markets` (status LIVE) ↔ `assets` ↔ LEFT JOIN `watch_config`:
baris tanpa override = enabled; baris `enabled=false` = dilewati.
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
    """Market LIVE aktif dipantau (default-enabled, override menonaktifkan)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        """Market LIVE + token Up/Down + condition id, urut end_at.

        Pasangan asetxtimeframe yang punya baris `watch_config`
        `enabled=false` (override pemilik) dikecualikan; sisanya —
        termasuk semua pasangan tanpa baris watch_config — dianggap
        enabled (ADR-012).
        """
        stmt = (
            select(MarketModel)
            .join(AssetModel, AssetModel.id == MarketModel.asset_id)
            .outerjoin(
                WatchConfigModel,
                (WatchConfigModel.asset_id == AssetModel.id)
                & (WatchConfigModel.timeframe == MarketModel.timeframe),
            )
            .where(
                MarketModel.status == MarketStatus.LIVE,
                # default-enabled: hanya override enabled=false yang
                # mengecualikan (watch_config NULL = aktif).
                WatchConfigModel.enabled.is_distinct_from(False),
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
