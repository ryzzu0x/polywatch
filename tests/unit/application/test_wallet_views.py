"""Unit test T-016: formatter /top & /wallet, validasi alamat, idempotensi
watch/unwatch dengan fake repository — tanpa DB/Telegram nyata.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from polywatch.application.ports import (
    UserRepository,
    WalletStatsRepository,
    WatchlistRepository,
)
from polywatch.application.use_cases.manage_watchlist import ManageWatchlist
from polywatch.application.use_cases.wallet_views import (
    DEFAULT_TOP,
    MAX_TOP,
    TopWallets,
    WalletDetail,
    short_wallet,
)
from polywatch.domain.errors import InvalidWalletError
from polywatch.domain.scoring import WalletStats

WALLET_A = "0x" + "ab" * 20
WALLET_B = "0x" + "cd" * 20
WALLET_MM = "0x" + "ee" * 20


def _stats(
    wallet: str,
    score: str = "80",
    markets: int = 120,
    wins: int = 80,
    pnl: str = "500",
    mm: bool = False,
) -> WalletStats:
    return WalletStats(
        wallet=wallet,
        markets_count=markets,
        wins=wins,
        avg_entry=Decimal("0.5"),
        pnl=Decimal(pnl),
        staked=Decimal("1000"),
        is_market_maker=mm,
        score=Decimal(score),
    )


# ---------------------------------------------------------------- formatter /top


def test_format_top_kosong() -> None:
    assert "Belum ada wallet" in TopWallets.format([])


def test_format_top_baris_lengkap() -> None:
    text = TopWallets.format([_stats(WALLET_A)])
    assert "Wallet skor tertinggi" in text
    assert short_wallet(WALLET_A) in text
    assert "skor 80" in text
    assert "120 market" in text
    assert "win 67%" in text


def test_format_top_edge_dan_roi_persen() -> None:
    stats = _stats(WALLET_A, score="70")
    text = TopWallets.format([stats])
    # edge = win_rate - avg_entry = 0.6667 - 0.5 = 16.7%; roi = 500/1000 = 50%
    assert "edge 16.7%" in text
    assert "ROI 50.0%" in text


# ---------------------------------------------------------------- formatter /wallet


def test_format_wallet_statistik_lengkap() -> None:
    stats = _stats(WALLET_A)
    text = WalletDetail.format(stats, watched=False)
    assert "Skor: 80" in text
    assert "Edge:" in text
    assert "ROI:" in text
    assert "PnL: 500 USDC" in text
    assert "Market: 120" in text
    assert "tidak di-watch" in text
    assert "Market maker: bukan" in text


def test_format_wallet_market_maker_catatan() -> None:
    stats = _stats(WALLET_MM, mm=True)
    text = WalletDetail.format(stats, watched=True)
    assert "di-watch" in text
    assert "Market maker: ya" in text
    assert "tidak masuk /top" in text


def test_short_wallet() -> None:
    assert short_wallet(WALLET_A) == "0xabab…abab"


# ---------------------------------------------------------------- use case + fake


class FakeWalletStatsRepo:
    def __init__(self, rows: list[WalletStats]) -> None:
        self._rows = rows
        self.calls: list[int] = []

    async def top(self, limit: int) -> list[WalletStats]:
        self.calls.append(limit)
        return self._rows[:limit]

    async def get(self, wallet: str) -> WalletStats | None:
        return next((r for r in self._rows if r.wallet == wallet), None)


class FakeWatchlistRepo:
    def __init__(self) -> None:
        self.items: set[tuple[int, str]] = set()

    async def add(self, user_id: int, wallet: str) -> None:
        self.items.add((user_id, wallet))

    async def remove(self, user_id: int, wallet: str) -> None:
        self.items.discard((user_id, wallet))

    async def list(self, user_id: int) -> list[str]:
        return [w for uid, w in self.items if uid == user_id]

    async def is_watched(self, user_id: int, wallet: str) -> bool:
        return (user_id, wallet) in self.items


class FakeUserRepo:
    def __init__(self) -> None:
        self._ids: dict[int, int] = {}

    async def get_id_by_telegram(self, telegram_id: int) -> int | None:
        return self._ids.get(telegram_id)

    async def get_or_create_by_telegram(self, telegram_id: int) -> int:
        if telegram_id not in self._ids:
            self._ids[telegram_id] = len(self._ids) + 1
        return self._ids[telegram_id]


async def test_top_default_dan_batas_limit() -> None:
    repo = FakeWalletStatsRepo([_stats(WALLET_A), _stats(WALLET_B)])
    use_case = TopWallets(cast(WalletStatsRepository, repo))
    await use_case.execute()
    assert repo.calls == [DEFAULT_TOP]
    await use_case.execute(999)
    assert repo.calls[-1] == MAX_TOP
    await use_case.execute(1)
    assert repo.calls[-1] == 1


async def test_wallet_belum_ada_statistik() -> None:
    use_case = WalletDetail(
        cast(WalletStatsRepository, FakeWalletStatsRepo([])),
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, FakeWatchlistRepo()),
    )
    text = await use_case.execute(42, WALLET_A)
    assert "belum punya statistik" in text


async def test_wallet_validasi_alamat_salah() -> None:
    use_case = WalletDetail(
        cast(WalletStatsRepository, FakeWalletStatsRepo([])),
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, FakeWatchlistRepo()),
    )
    with pytest.raises(InvalidWalletError):
        await use_case.execute(42, "bukan-alamat")


async def test_wallet_uppercase_dinormalisasi() -> None:
    repo = FakeWalletStatsRepo([_stats(WALLET_A)])
    use_case = WalletDetail(
        cast(WalletStatsRepository, repo),
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, FakeWatchlistRepo()),
    )
    text = await use_case.execute(42, WALLET_A.upper())
    assert "Skor: 80" in text


async def test_wallet_status_watchlist() -> None:
    watchlist = FakeWatchlistRepo()
    await watchlist.add(1, WALLET_A)
    use_case = WalletDetail(
        cast(WalletStatsRepository, FakeWalletStatsRepo([_stats(WALLET_A)])),
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, watchlist),
    )
    text = await use_case.execute(42, WALLET_A)
    assert "di-watch" in text


async def test_watch_unwatch_idempoten() -> None:
    watchlist = FakeWatchlistRepo()
    users = FakeUserRepo()
    use_case = ManageWatchlist(
        users,
        watchlist,
    )
    pertama = await use_case.watch(42, WALLET_A)
    kedua = await use_case.watch(42, WALLET_A)
    assert "masuk watchlist" in pertama
    assert "sudah ada di watchlist" in kedua
    assert watchlist.items == {(1, WALLET_A)}  # tidak duplikat

    out = await use_case.unwatch(42, WALLET_A)
    out_lagi = await use_case.unwatch(42, WALLET_A)
    assert "dikeluarkan dari watchlist" in out
    assert "memang tidak ada di watchlist" in out_lagi
    assert watchlist.items == set()


async def test_watch_validasi_alamat_salah() -> None:
    use_case = ManageWatchlist(
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, FakeWatchlistRepo()),
    )
    with pytest.raises(InvalidWalletError):
        await use_case.watch(42, "0x123")


async def test_unwatch_validasi_alamat_salah() -> None:
    use_case = ManageWatchlist(
        cast(UserRepository, FakeUserRepo()),
        cast(WatchlistRepository, FakeWatchlistRepo()),
    )
    with pytest.raises(InvalidWalletError):
        await use_case.unwatch(42, "0x123")
