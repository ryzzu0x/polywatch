"""Query token market live untuk ingest T-011/T-026/T-028.

Ingest berbasis JENDELA WAKTU (ADR-015/T-028), bukan status `LIVE` yang bisa
basi: market diambil bila `start_at <= now < end_at` dan statusnya bukan
`RESOLVED`. Status di DB bisa tertinggal (LIVE padahal `end_at` sudah lewat,
atau masih UPCOMING padahal jendelanya sudah berjalan) — jendela waktu
adalah sumber kebenaran; sweep status (T-028) hanya membereskan tampilan
status, bukan syarat ingest.

Margin `early_subscribe_s` (default 5 dtk): market yang mulai beberapa
detik lagi sudah di-subscribe supaya tidak ada event awal yang hilang.

Semantik watch default-enabled (ADR-012) tetap: market dikembalikan untuk
**semua** pasangan asetxtimeframe, KECUALI ada baris `watch_config`
override `enabled=false` untuk pasangan itu. Tabel `watch_config` kosong
berarti semua aset x timeframe aktif.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.ports import Clock
from polywatch.domain.market import MarketStatus, Timeframe
from polywatch.infrastructure.db.models import (
    AssetModel,
    MarketModel,
    WatchConfigModel,
)

#: Margin subscribe awal: market mulai <=5 dtk dari sekarang dianggap aktif.
DEFAULT_EARLY_SUBSCRIBE_SEC = 5.0


def _system_now() -> datetime:
    """Waktu sistem UTC (fallback bila clock tidak di-inject)."""
    return datetime.now(tz=UTC)


class SqlAlchemyLiveTokenQuery:
    """Market aktif menurut jendela waktu (default-enabled, override mematikan)."""

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock | None = None,
        early_subscribe_sec: float = DEFAULT_EARLY_SUBSCRIBE_SEC,
    ) -> None:
        self._session = session
        self._clock = clock
        self._early_subscribe = timedelta(seconds=max(early_subscribe_sec, 0.0))

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return _system_now()

    async def live_tokens(
        self, timeframes: Sequence[Timeframe] | None = None
    ) -> Sequence[TokenMarketInfo]:
        """Token Up/Down market aktif (`start_at <= now < end_at`), urut end_at.

        Kriteria jendela waktu (T-028):
        - `start_at - early_subscribe_sec <= now` — market yang mulai
          sebentar lagi sudah diambil (margin kecil);
        - `end_at > now` — market yang waktunya sudah selesai TIDAK
          diambil walau statusnya masih LIVE (status basi);
        - status != RESOLVED — market resolved tidak di-ingest ulang.

        Pasangan asetxtimeframe yang punya baris `watch_config`
        `enabled=false` (override pemilik) dikecualikan; sisanya —
        termasuk semua pasangan tanpa baris watch_config — dianggap
        enabled (ADR-012).
        """
        now = self._now()
        stmt = (
            select(MarketModel)
            .join(AssetModel, AssetModel.id == MarketModel.asset_id)
            .outerjoin(
                WatchConfigModel,
                (WatchConfigModel.asset_id == AssetModel.id)
                & (WatchConfigModel.timeframe == MarketModel.timeframe),
            )
            .where(
                MarketModel.start_at - self._early_subscribe <= now,
                MarketModel.end_at > now,
                MarketModel.status != MarketStatus.RESOLVED,
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


__all__ = [
    "DEFAULT_EARLY_SUBSCRIBE_SEC",
    "SqlAlchemyLiveTokenQuery",
]
