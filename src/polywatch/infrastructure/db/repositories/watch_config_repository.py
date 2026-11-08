"""Implementasi port WatchConfigRepository untuk PostgreSQL.

`watch_config` unik per (user_id, asset_id, timeframe): `set_enabled`
adalah upsert ON CONFLICT — memanggil berulang tidak menduplikasi. Aset
direferensikan lewat FK `assets.id` (dibuat discovery T-007).

Semantik default-enabled (T-026/ADR-012): baris watch_config hanya
menyimpan *override* — pasangan asetxtimeframe tanpa baris dianggap
enabled. `is_enabled` mengembalikan True untuk kombinasi tanpa baris
(sebelumnya False); `effective_watch_all` merangkum seluruh kombinasi
(aset terdaftar x timeframe) beserta status efektifnya untuk `/markets`
dan filter ingest.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.settings import WatchEntry
from polywatch.domain.market import Timeframe
from polywatch.infrastructure.db.models import AssetModel, WatchConfigModel

#: Semua timeframe yang didukung — dasar kombinasi default-enabled.
_ALL_TIMEFRAMES: tuple[Timeframe, ...] = tuple(Timeframe)


class SqlAlchemyWatchConfigRepository:
    """Repository watch_config dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _asset_id(self, asset: str) -> int | None:
        stmt = select(AssetModel.id).where(AssetModel.symbol == asset).limit(1)
        row = await self._session.scalar(stmt)
        return int(row) if row is not None else None

    async def asset_exists(self, asset: str) -> bool:
        stmt = select(exists().where(AssetModel.symbol == asset))
        return bool(await self._session.scalar(stmt))

    async def is_enabled(self, user_id: int, asset: str, timeframe: Timeframe) -> bool:
        """Status efektif pasangan asetxtimeframe (ADR-012).

        Tanpa baris override → True (default-enabled). Baris `enabled`
        mengikuti nilai baris (override bisa mematikan atau menghidupkan
        kembali).
        """
        asset_id = await self._asset_id(asset)
        if asset_id is None:
            return False  # aset tak terdaftar tidak bisa dipantau
        stmt = select(WatchConfigModel.enabled).where(
            WatchConfigModel.user_id == user_id,
            WatchConfigModel.asset_id == asset_id,
            WatchConfigModel.timeframe == timeframe,
        )
        row = await self._session.scalar(stmt)
        return bool(row) if row is not None else True

    async def set_enabled(
        self, user_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> None:
        asset_id = await self._asset_id(asset)
        if asset_id is None:
            raise ValueError(f"aset tidak dikenal: {asset!r}")
        stmt = (
            pg_insert(WatchConfigModel)
            .values(
                user_id=user_id,
                asset_id=asset_id,
                timeframe=timeframe,
                enabled=enabled,
            )
            .on_conflict_do_update(
                constraint="uq_watch_config",
                set_={"enabled": enabled},
            )
        )
        await self._session.execute(stmt)

    async def list_all(self, user_id: int) -> Sequence[WatchEntry]:
        """Semua baris override milik pengguna (bukan kombinasi efektif).

        Untuk daftar lengkap default-enabled gunakan `effective_watch_all`.
        """
        stmt = (
            select(WatchConfigModel, AssetModel)
            .join(AssetModel, WatchConfigModel.asset_id == AssetModel.id)
            .where(WatchConfigModel.user_id == user_id)
            .order_by(AssetModel.symbol, WatchConfigModel.timeframe)
        )
        result = await self._session.execute(stmt)
        return [
            WatchEntry(
                asset=row_asset.symbol,
                timeframe=row_config.timeframe,
                enabled=row_config.enabled,
            )
            for row_config, row_asset in result.all()
        ]

    async def effective_watch_all(self, user_id: int) -> Sequence[WatchEntry]:
        """Kombinasi efektif seluruh aset terdaftar x timeframe (ADR-012).

        Pasangan tanpa baris override = enabled (default). Urut symbol
        lalu timeframe — format yang sama dengan `list_all` sehingga
        `/markets` dan toggle konsisten.
        """
        stmt = select(AssetModel.symbol).order_by(AssetModel.symbol)
        symbols = (await self._session.scalars(stmt)).all()
        if not symbols:
            return []
        overrides: dict[tuple[str, Timeframe], bool] = {}
        override_stmt = (
            select(WatchConfigModel, AssetModel)
            .join(AssetModel, WatchConfigModel.asset_id == AssetModel.id)
            .where(WatchConfigModel.user_id == user_id)
        )
        rows = (await self._session.execute(override_stmt)).all()
        for row_config, row_asset in rows:
            overrides[(row_asset.symbol, row_config.timeframe)] = bool(row_config.enabled)
        return [
            WatchEntry(
                asset=symbol,
                timeframe=timeframe,
                enabled=overrides.get((symbol, timeframe), True),
            )
            for symbol in symbols
            for timeframe in _ALL_TIMEFRAMES
        ]
