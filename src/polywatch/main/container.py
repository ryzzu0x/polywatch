"""Composition root: merangkai adapter ke use case.

Hanya `main/` yang boleh meng-import semua layer. Container memegang
settings, clock, engine/session DB (dibuat lazy supaya `--check`
tidak membuka koneksi).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polywatch.application.ports import Clock
from polywatch.infrastructure.clock import SystemClock
from polywatch.main.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Buat engine SQLAlchemy async dari settings (asyncpg)."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )


@dataclass(slots=True)
class Container:
    """Objek yang dirakit sekali saat aplikasi start."""

    settings: Settings
    clock: Clock
    _engine: AsyncEngine | None = field(default=None, repr=False)
    _session_factory: async_sessionmaker[AsyncSession] | None = field(default=None, repr=False)

    @classmethod
    def build(cls, settings: Settings) -> Container:
        return cls(settings=settings, clock=SystemClock())

    @property
    def engine(self) -> AsyncEngine:
        """Engine DB, dibuat saat pertama kali dipakai."""
        if self._engine is None:
            self._engine = create_engine(self.settings)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        return self._session_factory

    async def aclose(self) -> None:
        """Tutup engine bila terbuka (shutdown rapi)."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


def build_container(settings: Settings) -> Container:
    """Fungsi kecil untuk dipakai entrypoint/test."""
    return Container.build(settings)


def session_kwargs(container: Container) -> dict[str, Any]:  # pragma: no cover
    """Kemudahan test: kwargs pembuat AsyncSession."""
    return {"bind": container.engine, "expire_on_commit": False}
