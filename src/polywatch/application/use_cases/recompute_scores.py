"""Use case recompute_scores: hitung ulang skor wallet setelah resolve (T-013).

Dipanggil setelah `market_id` resolve: untuk setiap wallet yang bertransaksi
di market itu, statistik dihitung ulang dari **seluruh** market yang sudah
resolve (bukan inkremental) lalu di-upsert ke `wallet_stats` (idempoten).

Perhitungan (win/loss, average entry berbobot USDC, PnL, staked, deteksi
market maker) ada di domain (`polywatch.domain.scoring`); use case hanya
merangkai data lewat port repository.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal

from polywatch.application.dto.trade import ResolvedTrade
from polywatch.application.ports import TradeRepository, WalletStatsRepository
from polywatch.domain.scoring import (
    MarketAggregate,
    ScoringPolicy,
    WalletStats,
    aggregate_market,
    build_wallet_stats,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecomputeSummary:
    """Hasil recompute satu market: wallet diproses dan skor tertinggi."""

    market_id: int
    wallets_processed: int
    top_wallet: str | None
    top_score: Decimal


@dataclass(slots=True)
class RecomputeScores:
    """Hitung ulang statistik & skor semua wallet yang trade di market resolve."""

    trades: TradeRepository
    wallet_stats: WalletStatsRepository
    policy: ScoringPolicy = field(default_factory=ScoringPolicy)

    async def execute(self, market_id: int) -> RecomputeSummary:
        """Recompute untuk semua wallet yang trade di `market_id`."""
        wallets = await self.trades.wallets_for_market(market_id)
        processed: list[WalletStats] = []
        for wallet in wallets:
            stats = await self._recompute_wallet(wallet)
            await self.wallet_stats.upsert(stats)
            processed.append(stats)

        top = max(processed, key=lambda s: s.score, default=None)
        summary = RecomputeSummary(
            market_id=market_id,
            wallets_processed=len(processed),
            top_wallet=top.wallet if top is not None and top.score > 0 else None,
            top_score=top.score if top is not None else Decimal(0),
        )
        logger.info(
            "recompute_scores selesai",
            extra={
                "event": "recompute_scores_done",
                "market_id": market_id,
                "wallets_processed": summary.wallets_processed,
                "top_score": str(summary.top_score),
            },
        )
        return summary

    async def _recompute_wallet(self, wallet: str) -> WalletStats:
        """Statistik wallet dari seluruh market yang sudah resolve."""
        resolved = await self.trades.resolved_trades_for_wallet(wallet)

        by_market: dict[int, list[ResolvedTrade]] = defaultdict(list)
        last_trade_at = None
        for item in resolved:
            by_market[item.trade.market_id].append(item)
            if item.last_trade_at is not None and (
                last_trade_at is None or item.last_trade_at > last_trade_at
            ):
                last_trade_at = item.last_trade_at

        aggregates: list[MarketAggregate] = [
            aggregate_market(
                market_id,
                [item.trade for item in items],
                winning=items[0].winning,
            )
            for market_id, items in by_market.items()
        ]
        stats = build_wallet_stats(wallet, aggregates, self.policy)
        return replace(stats, last_trade_at=last_trade_at)


__all__ = ["RecomputeScores", "RecomputeSummary"]
