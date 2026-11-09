"""Unit test recompute_scores dengan repository palsu (T-013)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.application.use_cases.recompute_scores import RecomputeScores
from polywatch.domain.market import Outcome
from polywatch.domain.scoring import ScoringPolicy, WalletStats
from polywatch.domain.trade import Action, Trade

_T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_WALLET_A = "0x" + "aa" * 20
_WALLET_B = "0x" + "bb" * 20


class FakeTradeRepository:
    """Repository palsu: trade per wallet pada market resolve."""

    def __init__(
        self, resolved: dict[str, list[ResolvedTrade]], market_wallets: dict[int, list[str]]
    ) -> None:
        self._resolved = resolved
        self._market_wallets = market_wallets

    async def insert_many(self, trades: Sequence[Trade]) -> int:
        return 0  # tidak dipakai recompute_scores

    async def last_traded_at(self, market_id: int) -> None:
        return None  # tidak dipakai recompute_scores

    async def wallets_for_market(self, market_id: int) -> list[str]:
        return self._market_wallets.get(market_id, [])

    async def resolved_trades_for_wallet(self, wallet: str) -> list[ResolvedTrade]:
        return self._resolved.get(wallet, [])

    async def list_wallets_with_resolved_trades(self) -> list[str]:
        return sorted(self._resolved)


class FakeWalletStatsRepository:
    def __init__(self) -> None:
        self.rows: dict[str, WalletStats] = {}
        self.upsert_calls = 0

    async def upsert(self, stats: WalletStats) -> None:
        self.upsert_calls += 1
        self.rows[stats.wallet] = stats

    async def top(self, limit: int) -> list[WalletStats]:
        return sorted(self.rows.values(), key=lambda s: s.score, reverse=True)[:limit]

    async def get(self, wallet: str) -> WalletStats | None:
        return self.rows.get(wallet)

    async def list_all(self) -> list[str]:
        return sorted(self.rows)


def _trade(
    market_id: int,
    wallet: str,
    outcome: Outcome,
    price: str,
    size: str,
    usdc: str,
    tx: int,
    action: Action = Action.BUY,
    offset_min: int = 0,
) -> ResolvedTrade:
    return ResolvedTrade(
        trade=Trade(
            tx_hash=f"0x{tx:064x}",
            log_index=tx,
            market_id=market_id,
            wallet=wallet,
            outcome=outcome,
            action=action,
            price=Decimal(price),
            size=Decimal(size),
            usdc=Decimal(usdc),
            traded_at=_T0 + timedelta(minutes=offset_min),
        ),
        winning=Outcome.UP if tx % 2 == 1 else Outcome.DOWN,
        last_trade_at=_T0 + timedelta(minutes=offset_min),
    )


def _buy_at(
    market_id: int,
    wallet: str,
    outcome: Outcome,
    winning: Outcome,
    price: str,
    usdc: str,
    offset_min: int,
) -> ResolvedTrade:
    """Satu buy `outcome` di market resolve dengan hasil `winning`."""
    return ResolvedTrade(
        trade=Trade(
            tx_hash=f"0x{market_id:064x}",
            log_index=market_id,
            market_id=market_id,
            wallet=wallet,
            outcome=outcome,
            action=Action.BUY,
            price=Decimal(price),
            size=Decimal(usdc) / Decimal(price),
            usdc=Decimal(usdc),
            traded_at=_T0 + timedelta(minutes=offset_min),
        ),
        winning=winning,
        last_trade_at=_T0 + timedelta(minutes=offset_min),
    )


async def test_recompute_tidak_melewati_wallet_tanpa_history() -> None:
    """Market tanpa trade → tidak ada upsert, tidak membagi nol."""
    trades = FakeTradeRepository({}, {})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(trades=trades, wallet_stats=stats_repo)

    summary = await use_case.execute(1)

    assert summary.wallets_processed == 0
    assert summary.top_wallet is None
    assert stats_repo.upsert_calls == 0


async def test_recompute_market_maker_dapat_skor_nol() -> None:
    """Wallet dua sisi di 2 dari 2 market → market maker → skor 0."""
    two_sided_market_1 = [
        _trade(1, _WALLET_A, Outcome.UP, "0.50", "100", "50", tx=1, offset_min=0),
        _trade(1, _WALLET_A, Outcome.DOWN, "0.50", "100", "50", tx=3, offset_min=1),
    ]
    two_sided_market_2 = [
        _trade(2, _WALLET_A, Outcome.UP, "0.50", "100", "50", tx=5, offset_min=2),
        _trade(2, _WALLET_A, Outcome.DOWN, "0.50", "100", "50", tx=7, offset_min=3),
    ]
    trades = FakeTradeRepository(
        resolved={_WALLET_A: two_sided_market_1 + two_sided_market_2},
        market_wallets={7: [_WALLET_A]},
    )
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=2),
    )

    summary = await use_case.execute(7)

    stats = stats_repo.rows[_WALLET_A]
    assert stats.is_market_maker is True
    assert stats.markets_count == 2
    assert stats.score == Decimal(0)
    assert summary.top_wallet is None  # semua skor 0


async def test_recompute_wallet_dengan_edge_positif_dapat_skor() -> None:
    """60 market, menang 48 (80%), entry 0.50 → edge 0.30, skor > 0."""
    resolved: list[ResolvedTrade] = []
    for m in range(1, 61):
        winning = Outcome.UP if m % 5 != 0 else Outcome.DOWN  # 48 menang, sisi UP
        resolved.append(_buy_at(m, _WALLET_A, Outcome.UP, winning, "0.50", "50", offset_min=m))

    trades = FakeTradeRepository(
        resolved={_WALLET_A: resolved},
        market_wallets={99: [_WALLET_A]},
    )
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=50),
    )

    await use_case.execute(99)

    stats = stats_repo.rows[_WALLET_A]
    assert stats.markets_count == 60
    assert stats.wins == 48  # 60 - 12 kalah
    assert stats.win_rate == Decimal("0.8")
    assert stats.avg_entry == Decimal("0.50")
    assert stats.edge == Decimal("0.30")
    # 48 kalahx(-50) & 12 menangx(+50): pnl = 48*50 - 12*50 = 1800
    assert stats.pnl == Decimal("48") * Decimal("50") - Decimal("12") * Decimal("50")
    assert stats.roi == Decimal("1800") / Decimal("3000")
    assert stats.score > Decimal(0)
    assert stats.is_market_maker is False


async def test_recompute_wallet_beli_mahal_edge_negatif_skor_nol() -> None:
    """Beli 0.90 menang 85% → edge negatif → skor 0 (kasus TASKS)."""
    # 9 kalah dari 60 → win rate 0.85, entry 0.90 → edge -0.05
    losing = {3, 10, 17, 24, 31, 38, 45, 52, 59}
    resolved = [
        _buy_at(
            m,
            _WALLET_B,
            Outcome.UP,
            Outcome.DOWN if m in losing else Outcome.UP,
            "0.90",
            "90",
            offset_min=m,
        )
        for m in range(1, 61)
    ]
    trades = FakeTradeRepository(
        resolved={_WALLET_B: resolved},
        market_wallets={61: [_WALLET_B]},
    )
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=50),
    )

    await use_case.execute(61)

    stats = stats_repo.rows[_WALLET_B]
    assert stats.win_rate == Decimal("0.85")
    assert stats.avg_entry == Decimal("0.90")
    assert stats.edge == Decimal("-0.05")
    assert stats.score == Decimal(0)


async def test_recompute_idempoten_dari_sisi_use_case() -> None:
    """Dua kali eksekusi dari data sumber sama → hasil identik."""
    resolved = [
        _buy_at(m, _WALLET_A, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
        for m in range(1, 61)
    ]
    trades = FakeTradeRepository(
        resolved={_WALLET_A: resolved},
        market_wallets={1: [_WALLET_A]},
    )
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=50),
    )

    first = await use_case.execute(1)
    first_row = stats_repo.rows[_WALLET_A]
    second = await use_case.execute(1)
    second_row = stats_repo.rows[_WALLET_A]

    assert first_row == second_row
    assert first == second
    assert stats_repo.upsert_calls == 2  # upsert dipanggil lagi, nilai sama


async def test_recompute_hanya_market_resolve() -> None:
    """Trade pada market belum resolve tidak ikut dihitung (port hanya beri resolved)."""
    trades = FakeTradeRepository(
        resolved={},  # tidak ada market resolve
        market_wallets={1: [_WALLET_A]},
    )
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(trades=trades, wallet_stats=stats_repo)

    await use_case.execute(1)

    stats = stats_repo.rows[_WALLET_A]
    assert stats.markets_count == 0
    assert stats.score == Decimal(0)
    assert stats.avg_entry is None
