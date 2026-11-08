"""Implementasi Clock dengan waktu sistem."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Jam sistem dalam UTC."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)
