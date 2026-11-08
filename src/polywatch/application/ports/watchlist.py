"""Port watchlist wallet per pengguna (T-016).

Tabel `watchlist`: pasangan (user_id, wallet). Idempoten — menambahkan
wallet yang sudah ada tidak mengubah apa pun.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class WatchlistRepository(Protocol):
    """Kelola daftar wallet yang di-watch satu pengguna."""

    async def add(self, user_id: int, wallet: str) -> None: ...

    async def remove(self, user_id: int, wallet: str) -> None: ...

    async def list(self, user_id: int) -> Sequence[str]: ...

    async def is_watched(self, user_id: int, wallet: str) -> bool: ...
