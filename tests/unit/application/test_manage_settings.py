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
    """WatchConfigRepository in-memory dengan unique key (user, asset, tf).

    Semantik ADR-012 (default-enabled): kombinasi efektif = semua aset
    terdaftar x timeframe; baris hanya menyimpan override statusnya.
    """

    def __init__(self, known_assets: set[str]) -> None:
        self.known_assets = known_assets
        self._rows: dict[tuple[int, str, Timeframe], bool] = {}

    async def is_enabled(self, user_id: int, asset: str, timeframe: Timeframe) -> bool:
        return self._rows.get((user_id, asset, timeframe), True)

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

    async def effective_watch_all(self, user_id: int) -> Sequence[WatchEntry]:
        return [
            WatchEntry(
                asset=asset,
                timeframe=timeframe,
                enabled=self._rows.get((user_id, asset, timeframe), True),
            )
            for asset in sorted(self.known_assets)
            for timeframe in Timeframe
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


async def test_list_watch_kosong_untuk_user_baru_tetap_semua_enabled() -> None:
    """T-026/ADR-012: tabel watch_config kosong = semua kombinasi enabled."""
    use_case, _, _ = _use_case()

    entries = await use_case.list_watch(999)
    # 3 aset x 2 timeframe, semuanya enabled (default-enabled)
    assert len(entries) == 6
    assert all(e.enabled for e in entries)
    assert {e.asset for e in entries} == {"btc", "eth", "sol"}
    assert {e.timeframe for e in entries} == set(Timeframe)


async def test_list_watch_override_mematikan_satu_kombinasi() -> None:
    """T-026: override enabled=false hanya mematikan kombinasi itu."""
    use_case, _, _ = _use_case()

    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, False)

    entries = await use_case.list_watch(101)
    by_key = {(e.asset, e.timeframe): e.enabled for e in entries}
    assert len(entries) == 6
    assert by_key[("btc", Timeframe.FIFTEEN_MINUTES)] is False
    assert by_key[("btc", Timeframe.FIVE_MINUTES)] is True
    assert by_key[("eth", Timeframe.FIFTEEN_MINUTES)] is True


async def test_enabled_assets_memfilter_yang_nonaktif() -> None:
    """Kriteria T-008 + T-026: nonaktif menyisakan kombinasi lain tetap aktif."""
    use_case, _, _ = _use_case()

    await use_case.set_watch_enabled(101, "btc", Timeframe.FIVE_MINUTES, True)
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, True)
    await use_case.set_watch_enabled(101, "eth", Timeframe.FIFTEEN_MINUTES, True)

    # nonaktifkan btcx15m — kombinasi lain tetap enabled (default-enabled)
    await use_case.set_watch_enabled(101, "btc", Timeframe.FIFTEEN_MINUTES, False)

    enabled = await use_case.enabled_assets(101)
    assert enabled == [
        WatchEntry(asset="btc", timeframe=Timeframe.FIVE_MINUTES, enabled=True),
        WatchEntry(asset="eth", timeframe=Timeframe.FIVE_MINUTES, enabled=True),
        WatchEntry(asset="eth", timeframe=Timeframe.FIFTEEN_MINUTES, enabled=True),
        WatchEntry(asset="sol", timeframe=Timeframe.FIVE_MINUTES, enabled=True),
        WatchEntry(asset="sol", timeframe=Timeframe.FIFTEEN_MINUTES, enabled=True),
    ]

    # simulasi filter siklus ingest: market dinonaktifkan tidak ikut
    all_markets = [("btc", Timeframe.FIVE_MINUTES), ("btc", Timeframe.FIFTEEN_MINUTES)]
    watched = {(e.asset, e.timeframe) for e in enabled}
    ingested = [m for m in all_markets if m in watched]
    assert ingested == [("btc", Timeframe.FIVE_MINUTES)]


async def test_enabled_assets_kosong_bila_semua_dinonaktifkan() -> None:
    """Override mematikan SEMUA kombinasi → tidak ada yang aktif."""
    use_case, watch, users = _use_case(known_assets={"sol"})
    user_id = await users.get_or_create_by_telegram(101)

    for timeframe in Timeframe:
        await watch.set_enabled(user_id, "sol", timeframe, False)

    assert await use_case.enabled_assets(101) == []
