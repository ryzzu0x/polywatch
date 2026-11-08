"""Unit test use case manage_settings (T-008) — fake repository, tanpa I/O."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from polywatch.application.dto.settings import WatchEntry
from polywatch.application.use_cases.manage_settings import (
    ManageSettings,
    UnknownAssetError,
)
from polywatch.domain.errors import InvalidAssetError
from polywatch.domain.market import Timeframe


class FakeUserRepository:
    """UserRepository in-memory; get_or_create idempoten per telegram_id."""

    def __init__(self) -> None:
        self._by_telegram: dict[int, int] = {}
        self._next_id = 1

    async def get_id_by_telegram(self, telegram_id: int) -> int | None:
        return self._by_telegram.get(telegram_id)

    async def get_or_create_by_telegram(self, telegram_id: int) -> int:
        if telegram_id not in self._by_telegram:
            self._by_telegram[telegram_id] = self._next_id
            self._next_id += 1
        return self._by_telegram[telegram_id]


class FakeWatchConfigRepository:
    """WatchConfigRepository in-memory dengan unique key (user, asset, tf)."""

    def __init__(self, known_assets: set[str]) -> None:
        self.known_assets = known_assets
        self._rows: dict[tuple[int, str, Timeframe], bool] = {}

    async def is_enabled(self, user_id: int, asset: str, timeframe: Timeframe) -> bool:
        return self._rows.get((user_id, asset, timeframe), False)

    async def set_enabled(
        self, user_id: int, asset: str, timeframe: Timeframe, enabled: bool
    ) -> None:
        self._rows[(user_id, asset, timeframe)] = enabled

    async def list_all(self, user_id: int) -> Sequence[WatchEntry]:
        return [
            WatchEntry(asset=asset, timeframe=timeframe, enabled=enabled)
            for (uid, asset, timeframe), enabled in sorted(self._rows.items())
            if uid == user_id
        ]

    async def asset_exists(self, asset: str) -> bool:
        return asset in self.known_assets


def _use_case(
    known_assets: set[str] | None = None,
) -> tuple[
    ManageSettings,
    FakeWatchConfigRepository,
    FakeUserRepository,
]:
    users = FakeUserRepository()
    watch = FakeWatchConfigRepository(known_assets or {"btc", "eth", "sol"})
    return ManageSettings(users=users, watch_config=watch), watch, users


async def test_set_watch_enabled_membuat_baris_user_dan_config() -> None:
    use_case, watch, users = _use_case()

    entry = await use_case.set_watch_enabled(101, "BTC", Timeframe.FIVE_MINUTES, True)

    assert entry == WatchEntry(asset="btc", timeframe=Timeframe.FIVE_MINUTES, enabled=True)
    assert await users.get_id_by_telegram(101) is not None  # user otomatis dibuat
    assert await watch.is_enabled(1, "btc", Timeframe.FIVE_MINUTES)


async def test_set_watch_toggle_off_lalu_on() -> None:
    use_case, watch, _ = _use_case()

    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, True)
    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, False)

    assert await watch.is_enabled(1, "eth", Timeframe.FIFTEEN_MINUTES) is False
    # satu baris saja — tidak duplikat per toggle
    assert len(list(await watch.list_all(1))) == 1


async def test_set_watch_idempoten_baik_user_maupun_config() -> None:
    use_case, watch, users = _use_case()

    await use_case.set_watch_enabled(101, "btc", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIVE_MINUTES, True)

    assert len(users._by_telegram) == 1
    assert len(list(await watch.list_all(1))) == 1


async def test_aset_tidak_terdaftar_ditolak() -> None:
    use_case, _, _ = _use_case(known_assets={"btc"})

    with pytest.raises(UnknownAssetError):
        await use_case.set_watch_enabled(101, "pepe", Timeframe.FIVE_MINUTES, True)


async def test_aset_rusak_ditolak_domain() -> None:
    use_case, _, _ = _use_case()

    with pytest.raises(InvalidAssetError):
        await use_case.set_watch_enabled(101, "bt c!", Timeframe.FIVE_MINUTES, True)


async def test_list_watch_kosong_untuk_user_baru() -> None:
    use_case, _, _ = _use_case()

    assert await use_case.list_watch(999) == []


async def test_list_watch_mengembalikan_semua_entri() -> None:
    use_case, _, _ = _use_case()

    await use_case.set_watch_enabled(101, "btc", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, False)

    entries = await use_case.list_watch(101)
    assert len(entries) == 2
    assert {e.asset for e in entries} == {"btc", "eth"}


async def test_enabled_assets_memfilter_yang_nonaktif() -> None:
    """Kriteria T-008: nonaktif menyisakan hanya market yang enabled."""
    use_case, _, _ = _use_case()

    await use_case.set_watch_enabled(101, "btc", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, True)
    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, True)

    # nonaktifkan btcx15m — siklus berikutnya hanya memproses sisanya
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, False)

    enabled = await use_case.enabled_assets(101)
    assert enabled == [
        WatchEntry(asset="btc", timeframe=Timeframe.FIVE_MINUTES, enabled=True),
        WatchEntry(asset="eth", timeframe=Timeframe.FIFTEEN_MINUTES, enabled=True),
    ]

    # simulasi filter siklus ingest: market dinonaktifkan tidak ikut
    all_markets = [("btc", Timeframe.FIVE_MINUTES), ("btc", Timeframe.FIFTEEN_MINUTES)]
    watched = {(e.asset, e.timeframe) for e in enabled}
    ingested = [m for m in all_markets if m in watched]
    assert ingested == [("btc", Timeframe.FIVE_MINUTES)]


async def test_enabled_assets_kosong_bila_semua_dinonaktifkan() -> None:
    use_case, _, _ = _use_case()

    await use_case.set_watch_enabled(101, "sol", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "sol", Timeframe.FIVE_MINUTES, False)

    assert await use_case.enabled_assets(101) == []
