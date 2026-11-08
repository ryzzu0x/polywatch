"""Composition root: merangkai adapter ke use case.

Adapter Polymarket dan database ditambahkan pada T-004/T-007;
sampai saat itu container hanya memegang konfigurasi dan clock
supaya entrypoint bisa dijalankan dan diuji.
"""

from __future__ import annotations

from dataclasses import dataclass

from polywatch.application.ports import Clock
from polywatch.infrastructure.clock import SystemClock
from polywatch.main.config import Settings


@dataclass(slots=True)
class Container:
    """Objek yang dirakit sekali saat aplikasi start."""

    settings: Settings
    clock: Clock

    @classmethod
    def build(cls, settings: Settings) -> Container:
        return cls(settings=settings, clock=SystemClock())
