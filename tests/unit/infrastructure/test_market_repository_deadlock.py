"""Unit test retry deadlock MarketRepository (T-026).

Membuktikan `_execute_with_deadlock_retry`:
- deadlock (DBAPIError dibungkus asyncpg DeadlockDetectedError / SQLSTATE
  40P01) di-retry sampai sukses — siklus tidak gagal;
- error non-deadlock diteruskan langsung (tidak ada retry buta);
- deadlock yang bertahan melebihi batas retry diteruskan setelah log
  `db_deadlock_exhausted`.

Tanpa I/O nyata: session palsu mencatat rollback; sleep di-inject lewat
monkeypatch modul supaya test cepat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError

import polywatch.infrastructure.db.repositories.market_repository as market_repository_module
from polywatch.infrastructure.db.repositories.market_repository import (
    SqlAlchemyMarketRepository,
    _is_deadlock,
)


class _FakeAsyncpgDeadlock(Exception):
    """Meniru asyncpg.exceptions.DeadlockDetectedError."""

    sqlstate = "40P01"


class _FakeSession:
    """Session palsu: mencatat rollback, tanpa DB."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _wrapped(exc: BaseException) -> DBAPIError:
    """Bungkus exception driver seperti yang dilakukan SQLAlchemy asyncpg."""
    return DBAPIError("stmt", {}, exc)


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)


def test_is_deadlock_mendeteksi_sqlstate_40p01() -> None:
    assert _is_deadlock(_wrapped(_FakeAsyncpgDeadlock())) is True

    # nama kelas asyncpg asli (tanpa sqlstate) — fallback nama kelas
    DeadlockDetectedError = type("DeadlockDetectedError", (Exception,), {})
    assert _is_deadlock(_wrapped(DeadlockDetectedError())) is True


def test_is_deadlock_menolak_error_lain() -> None:
    assert _is_deadlock(RuntimeError("boom")) is False
    assert _is_deadlock(_wrapped(ValueError("bukan deadlock"))) is False


async def test_upsert_deadlock_di_retry_sampai_sukses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deadlock 2x lalu sukses → statement tetap tersimpan, siklus jalan."""
    repo = SqlAlchemyMarketRepository(_FakeSession())  # type: ignore[arg-type]
    calls = 0

    async def _execute() -> int:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _wrapped(_FakeAsyncpgDeadlock())
        return 42

    with caplog.at_level(logging.WARNING, logger=market_repository_module.__name__):
        result = await repo._execute_with_deadlock_retry("upsert_test", _execute)

    assert result == 42
    assert calls == 3
    retry_logs = [r for r in caplog.records if getattr(r, "event", None) == "db_deadlock_retry"]
    assert len(retry_logs) == 2  # log terstruktur tiap retry
    session = cast("_FakeSession", repo._session)
    assert session.rollbacks == 2  # rollback sebelum tiap retry


async def test_upsert_error_bukan_deadlock_diteruskan() -> None:
    repo = SqlAlchemyMarketRepository(_FakeSession())  # type: ignore[arg-type]
    calls = 0

    async def _execute() -> int:
        nonlocal calls
        calls += 1
        raise _wrapped(ValueError("unique violation"))

    with pytest.raises(DBAPIError):
        await repo._execute_with_deadlock_retry("upsert_test", _execute)
    assert calls == 1  # tidak ada retry untuk error biasa


async def test_upsert_deadlock_habis_retry_diteruskan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deadlock menetap > DEADLOCK_MAX_RETRIES → diteruskan (di-log error)."""
    repo = SqlAlchemyMarketRepository(_FakeSession())  # type: ignore[arg-type]
    calls = 0

    async def _execute() -> int:
        nonlocal calls
        calls += 1
        raise _wrapped(_FakeAsyncpgDeadlock())

    with (
        caplog.at_level(logging.ERROR, logger=market_repository_module.__name__),
        pytest.raises(DBAPIError),
    ):
        await repo._execute_with_deadlock_retry("upsert_test", _execute)

    assert calls == market_repository_module.DEADLOCK_MAX_RETRIES
    assert any(getattr(r, "event", None) == "db_deadlock_exhausted" for r in caplog.records)
