"""Unit test `RecomputeScores.recompute_all` (T-032 backfill).

Pemilihan kandidat (union `wallet_stats.list_all` x wallet dengan trade
resolve) + perhitungan lewat jalur domain yang sama, `--limit`, dan
`--dry-run`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.application.use_cases.recompute_scores import RecomputeScores
from polywatch.domain.market import Outcome
from polywatch.domain.scoring import ScoringPolicy, WalletStats

from .test_recompute_scores import (
    _WALLET_A,
    _WALLET_B,
    FakeTradeRepository,
    FakeWalletStatsRepository,
    _buy_at,
)

_WALLET_C = "0x" + "cc" * 20


def _scored_stats(wallet: str, score: Decimal) -> WalletStats:
    return WalletStats(
        wallet=wallet,
        markets_count=11,
        wins=9,
        avg_entry=Decimal("0.50"),
        pnl=Decimal("100"),
        staked=Decimal("550"),
        score=score,
    )


async def test_recompute_all_union_kandidat() -> None:
    """Kandidat = union baris wallet_stats lama + wallet dengan trade resolve.

    _WALLET_A hanya ada di trade resolve (belum pernah dihitung), _WALLET_B
    hanya punya baris wallet_stats lama (tanpa trade resolve) — keduanya
    diproses.
    """
    resolved = [
        _buy_at(m, _WALLET_A, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
        for m in range(1, 12)  # 11 market menang semua
    ]
    trades = FakeTradeRepository(resolved={_WALLET_A: resolved}, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    await stats_repo.upsert(_scored_stats(_WALLET_B, Decimal(0)))
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    summary = await use_case.recompute_all()

    assert summary.wallets_processed == 2  # A (dari trade) + B (dari wallet_stats)
    assert summary.wallets_scored == 1  # hanya A; B 0 market → 0
    assert summary.top_wallet == _WALLET_A
    assert summary.top_score > 0
    assert stats_repo.rows[_WALLET_A].markets_count == 11
    assert stats_repo.rows[_WALLET_A].score > 0


async def test_recompute_all_wallet_market_maker_skor_nol() -> None:
    """Wallet dua sisi di semua market → market maker → skor 0."""
    two_sided = []
    for m in range(1, 12):
        two_sided.append(_buy_at(m, _WALLET_B, Outcome.UP, Outcome.UP, "0.50", "50", m))
        two_sided.append(_buy_at(m, _WALLET_B, Outcome.DOWN, Outcome.UP, "0.50", "50", m + 100))
    trades = FakeTradeRepository(resolved={_WALLET_B: two_sided}, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    summary = await use_case.recompute_all()

    assert summary.wallets_processed == 1
    assert summary.wallets_scored == 0
    stats = stats_repo.rows[_WALLET_B]
    assert stats.is_market_maker is True
    assert stats.score == Decimal(0)


async def test_recompute_all_dry_run_tidak_menulis() -> None:
    """`--dry-run`: kandidat dihitung tapi wallet_stats tidak ditulis."""
    resolved = [
        _buy_at(m, _WALLET_A, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
        for m in range(1, 12)
    ]
    trades = FakeTradeRepository(resolved={_WALLET_A: resolved}, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    summary = await use_case.recompute_all(dry_run=True)

    assert summary.dry_run is True
    assert summary.wallets_processed == 1
    assert summary.wallets_scored == 1
    assert stats_repo.rows == {}  # tidak ada tulisan
    assert stats_repo.upsert_calls == 0


async def test_recompute_all_limit_membatasi_batch_pertama() -> None:
    """>2 kandidat + limit 2 → hanya 2 wallet pertama (urut) diproses."""
    resolved = {
        wallet: [
            _buy_at(m, wallet, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
            for m in range(1, 12)
        ]
        for wallet in (_WALLET_A, _WALLET_B, _WALLET_C)
    }
    trades = FakeTradeRepository(resolved=resolved, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    summary = await use_case.recompute_all(limit=2)

    assert summary.wallets_processed == 2
    assert _WALLET_C not in stats_repo.rows  # batch pertama = A, B (urut)


async def test_recompute_all_idempoten() -> None:
    """Dua kali recompute_all dari sumber sama → hasil identik."""
    resolved = [
        _buy_at(m, _WALLET_A, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
        for m in range(1, 12)
    ]
    trades = FakeTradeRepository(resolved={_WALLET_A: resolved}, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    first = await use_case.recompute_all()
    row_first = stats_repo.rows[_WALLET_A]
    second = await use_case.recompute_all()
    row_second = stats_repo.rows[_WALLET_A]

    assert row_first == row_second
    assert first.wallets_processed == second.wallets_processed
    assert first.wallets_scored == second.wallets_scored
    assert first.top_score == second.top_score


async def test_recompute_all_diproses_satu_per_satu() -> None:
    """`resolved_trades_for_wallet` dipanggil sekali per wallet — tidak ada
    pemuatan seluruh trade sekaligus (perilaku observabel lewat fake)."""
    resolved = [
        _buy_at(m, _WALLET_A, Outcome.UP, Outcome.UP, "0.50", "50", offset_min=m)
        for m in range(1, 12)
    ]
    trades = FakeTradeRepository(resolved={_WALLET_A: resolved}, market_wallets={})
    stats_repo = FakeWalletStatsRepository()
    use_case = RecomputeScores(
        trades=trades,
        wallet_stats=stats_repo,
        policy=ScoringPolicy(min_sample=10),
    )

    calls: list[str] = []
    original = trades.resolved_trades_for_wallet

    async def _counting(wallet: str) -> Sequence[ResolvedTrade]:
        calls.append(wallet)
        result: Sequence[ResolvedTrade] = await original(wallet)
        return list(result)

    trades.resolved_trades_for_wallet = _counting  # type: ignore[method-assign,assignment]
    await use_case.recompute_all()

    assert calls == [_WALLET_A]
