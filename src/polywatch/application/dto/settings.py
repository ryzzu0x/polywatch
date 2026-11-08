"""DTO pengaturan watch asetxtimeframe per pengguna."""

from __future__ import annotations

from dataclasses import dataclass

from polywatch.domain.market import Timeframe


@dataclass(frozen=True, slots=True)
class WatchEntry:
    """Satu baris watch_config sebagaimana dilihat use case."""

    asset: str
    timeframe: Timeframe
    enabled: bool
