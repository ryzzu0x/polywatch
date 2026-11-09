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
import time
from collections import defaultdict
from collections.abc import Callable
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


@dataclass(frozen=True, slots=True)
class RecomputeAllSummary:
    """Hasil recompute_all: wallet diproses dan distribusi skor (T-032)."""

    wallets_processed: int
    wallets_scored: int
    top_score: Decimal
    top_wallet: str | None
    duration_sec: float
    dry_run: bool = False


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

    async def recompute_all(
        self,
        limit: int | None = None,
        dry_run: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> RecomputeAllSummary:
        """Hitung ulang SEMUA wallet kandidat (T-032 backfill).

        Kandidat = union wallet yang sudah punya baris `wallet_stats` dan
        wallet yang punya trade di market RESOLVED. Tiap wallet diproses
        satu per satu lewat jalur domain yang sama dengan `execute`
        (`resolved_trades_for_wallet` → `aggregate_market` →
        `build_wallet_stats`) — hanya trade satu wallet yang dimuat ke
        memori pada satu waktu.

        `limit` membatasi jumlah wallet (batch pertama, urut wallet);
        `dry_run` menghitung tanpa menulis ke `wallet_stats`.
        `clock` (monotonik, detik) untuk durasi ringkasan; default
        `time.perf_counter`.
        """
        started = (clock or time.perf_counter)()
        from_stats = await self.wallet_stats.list_all()
        from_trades = await self.trades.list_wallets_with_resolved_trades()
        candidates = sorted({*from_stats, *from_trades})
        if limit is not None and limit >= 0:
            candidates = candidates[:limit]

        processed = 0
        scored = 0
        top_wallet: str | None = None
        top_score = Decimal(0)
        for wallet in candidates:
            stats = await self._recompute_wallet(wallet)
            if not dry_run:
                await self.wallet_stats.upsert(stats)
            processed += 1
            if stats.score > 0:
                scored += 1
                if stats.score > top_score:
                    top_score = stats.score
                    top_wallet = stats.wallet

        duration = (clock or time.perf_counter)() - started
        summary = RecomputeAllSummary(
            wallets_processed=processed,
            wallets_scored=scored,
            top_score=top_score,
            top_wallet=top_wallet,
            duration_sec=duration,
            dry_run=dry_run,
        )
        logger.info(
            "recompute_all selesai",
            extra={
                "event": "recompute_all_done",
                "wallets_processed": summary.wallets_processed,
                "wallets_scored": summary.wallets_scored,
                "top_score": str(summary.top_score),
                "dry_run": dry_run,
                "duration_sec": round(duration, 3),
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


__all__ = [
    "RecomputeAllSummary",
    "RecomputeScores",
    "RecomputeSummary",
]
