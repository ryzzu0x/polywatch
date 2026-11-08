"""Implementasi port AlertSettingsRepository & AlertsSentRepository (T-017).

`alert_settings`: satu baris per user (upsert), kolom min_score, min_usdc,
cooldown_sec, enabled. `alerts_sent`: append-only riwayat alert dengan
delay_ms untuk audit latency.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from polywatch.infrastructure.db.models import AlertSentModel, AlertSettingsModel


class SqlAlchemyAlertSettingsRepository:
    """Repository alert_settings dengan session yang dikelola pemanggil."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> dict[str, object]:
        stmt = select(AlertSettingsModel).where(AlertSettingsModel.user_id == user_id)
        row = await self._session.scalar(stmt)
        if row is None:
            return {}
        return {
            "min_score": row.min_score,
            "min_usdc": row.min_usdc,
            "cooldown_sec": row.cooldown_sec,
            "enabled": row.enabled,
        }

    async def save(self, user_id: int, values: dict[str, object]) -> None:
        stmt = (
            pg_insert(AlertSettingsModel)
            .values(
                user_id=user_id,
                min_score=values["min_score"],
                min_usdc=values["min_usdc"],
                cooldown_sec=values["cooldown_sec"],
                enabled=values["enabled"],
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "min_score": values["min_score"],
                    "min_usdc": values["min_usdc"],
                    "cooldown_sec": values["cooldown_sec"],
                    "enabled": values["enabled"],
                },
            )
        )
        await self._session.execute(stmt)


class SqlAlchemyAlertsSentRepository:
    """Repository alerts_sent (append + query cooldown)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        user_id: int,
        wallet: str,
        market_id: int,
        tx_hash: str,
        log_index: int,
        sent_at: datetime,
        delay_ms: int,
    ) -> None:
        """Simpan riwayat alert; `trade_id` diisi lewat kunci idempoten trade."""
        from polywatch.infrastructure.db.models import TradeModel

        trade_id = await self._session.scalar(
            select(TradeModel.id).where(
                TradeModel.tx_hash == tx_hash,
                TradeModel.log_index == log_index,
                TradeModel.wallet == wallet,
            )
        )
        self._session.add(
            AlertSentModel(
                user_id=user_id,
                wallet=wallet,
                market_id=market_id,
                trade_id=trade_id,
                sent_at=sent_at,
                delay_ms=delay_ms,
            )
        )

    async def last_sent_at(self, user_id: int, wallet: str) -> datetime | None:
        stmt = select(func.max(AlertSentModel.sent_at)).where(
            AlertSentModel.user_id == user_id,
            AlertSentModel.wallet == wallet,
        )
        return await self._session.scalar(stmt)
