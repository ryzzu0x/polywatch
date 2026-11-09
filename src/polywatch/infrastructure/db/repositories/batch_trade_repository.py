"""Repository trade dengan session baru per operasi (T-031/BUG-015).

Soak test T-030 menunjukkan `PendingRollbackError` berantai: ingest
memakai SATU scoped session seumur loop; begitu satu `insert_many`
gagal, transaksi session invalid dan tidak pernah di-rollback — semua
penyimpanan berikutnya gagal sampai proses restart (14x
`ingest_stream_error`, stream restart tiap ~35 dtk, `trades` beku).

Perbaikan (ADR-017): unit-of-work per batch — setiap operasi tulis
membuka session baru dari factory, commit saat sukses, dan SELALU
rollback + close di `finally`. Batch yang gagal tidak menular ke batch
berikutnya; kegagalan ditangkap pemanggil (use case me-log terstruktur
dan melanjutkan stream).

Implementasi meng-delegasikan ke `SqlAlchemyTradeRepository` per session
— logika SQL tidak diduplikasi.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.domain.trade import Trade
from polywatch.infrastructure.db.repositories.trade_repository import (
    SqlAlchemyTradeRepository,
)

#: Tipe factory session (contoh: `Container.session_factory`).
SessionFactory = Callable[..., AsyncSession]


class BatchSessionTradeRepository:
    """Repository trade dengan session sekali-pakai per operasi.

    Dipakai ingest kontinyu: loop WS hidup berjam-jam, jadi tidak boleh
    menahan satu `AsyncSession` (transaksi invalid menular ke semua batch
    berikutnya — BUG-015). Setiap metode membuka session baru, commit bila
    sukses, dan menutup di `finally` apa pun hasilnya.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        """Sisipkan batch idempoten pada session baru; commit saat sukses."""
        async with self._session_factory() as session:
            try:
                repo = SqlAlchemyTradeRepository(session)
                saved = await repo.insert_many(trades)
                await session.commit()
                return saved
            except Exception:
                await session.rollback()
                raise

    async def last_traded_at(self, market_id: int) -> datetime | None:
        async with self._session_factory() as session:
            return await SqlAlchemyTradeRepository(session).last_traded_at(market_id)

    async def wallets_for_market(self, market_id: int) -> Sequence[str]:
        async with self._session_factory() as session:
            return await SqlAlchemyTradeRepository(session).wallets_for_market(market_id)

    async def resolved_trades_for_wallet(self, wallet: str) -> Sequence[ResolvedTrade]:
        async with self._session_factory() as session:
            return await SqlAlchemyTradeRepository(session).resolved_trades_for_wallet(wallet)

    async def list_wallets_with_resolved_trades(self) -> Sequence[str]:
        async with self._session_factory() as session:
            return await SqlAlchemyTradeRepository(session).list_wallets_with_resolved_trades()
