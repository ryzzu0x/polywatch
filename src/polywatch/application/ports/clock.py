"""Port waktu — supaya use case bisa dites tanpa waktu nyata."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Sumber waktu sekarang (UTC)."""

    def now(self) -> datetime: ...
