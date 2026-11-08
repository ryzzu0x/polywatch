"""Implementasi port MarketRepository untuk PostgreSQL (SQLAlchemy 2 async).

Upsert idempoten: `markets` ON CONFLICT (condition_id) — menjalankan
discover dua kali tidak menambah baris. Aset baru dari slug dibuat otomatis
(idempoten via ON CONFLICT (symbol) di tabel `assets`). Status tidak pernah
mundur: baris existing dengan status lebih maju tetap dipertahankan.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Case, case, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from polywatch.application.dto.market import (
    MarketResultEntry,
    PendingMarket,
    TokenMarketInfo,
)
from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import (
    Market,
    MarketResult,
    MarketSlug,
    MarketStatus,
    Timeframe,
)
from polywatch.infrastructure.db.models import (
    AssetModel,
    MarketModel,
    MarketResultModel,
)

_STATUS_RANK: dict[MarketStatus, int] = {
    MarketStatus.UPCOMING: 0,
    MarketStatus.LIVE: 1,
    MarketStatus.CLOSED: 2,
    MarketStatus.RESOLVED: 3,
}


def _slug_str(market: Market) -> str:
    """Rekonstruksi slug dari domain (sumber kebenaran format)."""
    return (
        f"{market.slug.asset.symbol}-updown-{market.slug.timeframe.value}"
        f"-{int(market.slug.start_at.timestamp())}"
    )


class SqlAlchemyMarketRepository:
    """Repository market dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, market: Market) -> int:
        """Upsert satu market; kembalikan id baris (baru maupun lama)."""
        asset_id = await self._ensure_asset(market.slug.asset.symbol)

        stmt = (
            pg_insert(MarketModel)
            .values(
                condition_id=market.condition_id,
                slug=_slug_str(market),
                asset_id=asset_id,
                timeframe=market.slug.timeframe,
                start_at=market.slug.start_at,
                end_at=market.slug.end_at,
                token_up=market.token_up,
                token_down=market.token_down,
                status=market.status.name,
            )
            .on_conflict_do_update(
                index_elements=["condition_id"],
                set_={
                    "token_up": market.token_up,
                    "token_down": market.token_down,
                    # status tidak mundur: status existing dipakai bila lebih maju.
                    # Bind param di SET ON CONFLICT tidak melewati konversi tipe
                    # Enum, jadi wajib dikirim sebagai nama enum (uppercase).
                    "status": case(
                        (
                            _status_rank(MarketModel.status) > _STATUS_RANK[market.status],
                            MarketModel.status,
                        ),
                        else_=market.status.name,
                    ),
                },
            )
            .returning(MarketModel.id)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def get_by_slug(self, slug: str) -> Market | None:
        stmt = (
            select(MarketModel, AssetModel)
            .join(AssetModel, MarketModel.asset_id == AssetModel.id)
            .where(MarketModel.slug == slug)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        market_row, _ = cast("tuple[MarketModel, AssetModel]", row)
        return _to_domain(market_row)

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        stmt = (
            select(MarketModel, AssetModel)
            .join(AssetModel, MarketModel.asset_id == AssetModel.id)
            .where(MarketModel.status == status)
            .order_by(MarketModel.start_at)
        )
        result = await self._session.execute(stmt)
        rows = cast("Sequence[tuple[MarketModel, AssetModel]]", result.all())
        return [_to_domain(m) for m, _ in rows]

    async def count_by_status(self, status: MarketStatus) -> int:
        """Jumlah market berstatus tertentu (untuk /status)."""
        stmt = select(func.count()).select_from(MarketModel).where(MarketModel.status == status)
        return int(await self._session.scalar(stmt) or 0)

    async def latest_results(
        self, asset: str, timeframe: Timeframe, limit: int
    ) -> Sequence[MarketResultEntry]:
        """Hasil market terbaru satu aset x timeframe (untuk /results).

        Join `markets` x `market_results` x `assets`, urut start_at terbaru,
        dibatasi `limit`.
        """
        stmt = (
            select(
                MarketModel.slug,
                MarketResultModel.result,
                MarketResultModel.open_price,
                MarketResultModel.close_price,
                MarketResultModel.volume_usdc,
                MarketModel.start_at,
            )
            .join(MarketResultModel, MarketResultModel.market_id == MarketModel.id)
            .join(AssetModel, AssetModel.id == MarketModel.asset_id)
            .where(AssetModel.symbol == asset, MarketModel.timeframe == timeframe)
            .order_by(MarketModel.start_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            MarketResultEntry(
                slug=row.slug,
                result=row.result,
                open_price=row.open_price,
                close_price=row.close_price,
                volume_usdc=row.volume_usdc,
                start_at=row.start_at,
            )
            for row in result.all()
        ]

    async def get_result(self, market_id: int) -> MarketResult | None:
        stmt = select(MarketResultModel).where(MarketResultModel.market_id == market_id)
        row = await self._session.scalar(stmt)
        if row is None:
            return None
        return MarketResult(
            market_id=row.market_id,
            open_price=row.open_price,
            close_price=row.close_price,
            result=row.result,
            volume_usdc=row.volume_usdc,
            price_source=row.price_source,
            resolved_at=row.resolved_at,
        )

    async def save_result(self, result: MarketResult) -> None:
        """Simpan market_results idempoten (market_id = PK): ON CONFLICT DO NOTHING."""
        stmt = (
            pg_insert(MarketResultModel)
            .values(
                market_id=result.market_id,
                open_price=result.open_price,
                close_price=result.close_price,
                result=result.result,
                volume_usdc=result.volume_usdc,
                price_source=result.price_source,
                resolved_at=result.resolved_at,
            )
            .on_conflict_do_nothing(index_elements=["market_id"])
        )
        await self._session.execute(stmt)

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        """Perbarui status market (validasi transisi dilakukan use case/domain)."""
        stmt = update(MarketModel).where(MarketModel.id == market_id).values(status=status.name)
        await self._session.execute(stmt)

    async def has_result(self, market_id: int) -> bool:
        stmt = select(exists().where(MarketResultModel.market_id == market_id))
        return bool(await self._session.scalar(stmt))

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        """Token Up/Down + condition id market (untuk ingest/backfill T-011)."""
        stmt = select(MarketModel).where(MarketModel.id == market_id)
        row = await self._session.scalar(stmt)
        if row is None:
            return None
        return TokenMarketInfo(
            market_id=row.id,
            condition_id=row.condition_id,
            token_up=row.token_up,
            token_down=row.token_down,
            slug=row.slug,
        )

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        """Market CLOSED yang belum punya market_results (urut end_at lama dulu)."""
        stmt = (
            select(MarketModel)
            .where(
                MarketModel.status == MarketStatus.CLOSED,
                ~exists().where(MarketResultModel.market_id == MarketModel.id),
            )
            .order_by(MarketModel.end_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [
            PendingMarket(
                market_id=row.id,
                condition_id=row.condition_id,
                slug=row.slug,
                status=row.status,
            )
            for row in rows
        ]

    async def _ensure_asset(self, symbol: str) -> int:
        """Buat asset bila belum ada (idempoten); kembalikan id-nya."""
        stmt = (
            pg_insert(AssetModel)
            .values(symbol=symbol, name=symbol.upper(), enabled_default=True)
            .on_conflict_do_update(index_elements=["symbol"], set_={"name": symbol.upper()})
            .returning(AssetModel.id)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_symbols(self) -> list[str]:
        """Semua simbol aset yang sudah dikenal (port AssetSource, T-024)."""
        result = await self._session.scalars(select(AssetModel.symbol).order_by(AssetModel.symbol))
        return list(result.all())


def _status_rank(column: InstrumentedAttribute[MarketStatus]) -> Case[Any]:
    """Ekspresi SQL rank status (upcoming=0 ... resolved=3) untuk kolom status.

    Perbandingan memakai nama enum (uppercase) karena nilai bind param enum
    Python dirender asyncpg sebagai `str()` lowercase, bukan `.name`.
    """
    return case(
        (column == MarketStatus.UPCOMING.name, 0),
        (column == MarketStatus.LIVE.name, 1),
        (column == MarketStatus.CLOSED.name, 2),
        (column == MarketStatus.RESOLVED.name, 3),
        else_=-1,
    )


def _to_domain(market_row: MarketModel) -> Market:
    try:
        slug = MarketSlug.parse(market_row.slug)
    except InvalidMarketSlugError as exc:
        raise ValueError(f"slug di DB tidak valid: {market_row.slug!r}") from exc
    return Market(
        condition_id=market_row.condition_id,
        slug=slug,
        token_up=market_row.token_up,
        token_down=market_row.token_down,
        status=market_row.status,
    )
