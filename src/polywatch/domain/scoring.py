"""Penilaian wallet: edge dan ROI, bukan win rate mentah (ADR-003).

Skor 0-100 = kombinasi edge & ROI ter-shrink (disusutkan untuk sampel
kecil). Wallet market maker (rutin memegang dua sisi) dan wallet tanpa
sampel cukup mendapat skor 0. Seluruh parameter ada di `ScoringPolicy`
yang bisa di-inject, tidak di-hardcode di fungsi.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from polywatch.domain.market import Outcome
from polywatch.domain.trade import Trade

MIN_SAMPLE_MARKETS = 50
DEFAULT_MIN_SCORE = 60.0

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Parameter penilaian wallet — di-inject, tidak di-hardcode.

    - `min_sample`: jumlah market resolve minimum sebelum skor dihitung.
    - `shrinkage_k`: konstanta penyusutan; skor efektif = nilai x n/(n+k).
    - `edge_weight`/`roi_weight`: bobot kombinasi (dinormalisasi totalnya).
    - `edge_scale`/`roi_scale`: nilai ter-shrink yang dianggap "maksimal"
      (edge 0.20 dan ROI 100% sudah skor penuh pada komponennya).
    - `score_threshold`: ambang skor normal (0-100) untuk laporan/alert.
    - `market_maker_ratio`: porsi market dua sisi yang menandai market maker.
    """

    min_sample: int = MIN_SAMPLE_MARKETS
    shrinkage_k: Decimal = Decimal(50)
    edge_weight: Decimal = Decimal("0.5")
    roi_weight: Decimal = Decimal("0.5")
    edge_scale: Decimal = Decimal("0.20")
    roi_scale: Decimal = Decimal(1)
    score_threshold: Decimal = Decimal(60)
    market_maker_ratio: Decimal = Decimal("0.30")


@dataclass(frozen=True, slots=True)
class WalletStats:
    """Ringkasan performa wallet pada market yang sudah resolve."""

    wallet: str
    markets_count: int
    wins: int
    avg_entry: Decimal | None
    pnl: Decimal
    staked: Decimal
    is_market_maker: bool = False
    score: Decimal = _ZERO
    last_trade_at: datetime | None = None

    @property
    def win_rate(self) -> Decimal:
        if self.markets_count <= 0:
            return Decimal(0)
        return Decimal(self.wins) / Decimal(self.markets_count)

    @property
    def edge(self) -> Decimal:
        """Selisih win rate terhadap harga masuk rata-rata.

        Beli di 0.90 lalu menang 85% menghasilkan edge negatif.
        """
        if self.avg_entry is None or self.markets_count <= 0:
            return Decimal(0)
        return self.win_rate - self.avg_entry

    @property
    def roi(self) -> Decimal:
        if self.staked <= 0:
            return Decimal(0)
        return self.pnl / self.staked

    @property
    def has_enough_sample(self) -> bool:
        return self.markets_count >= MIN_SAMPLE_MARKETS

    @property
    def is_alertable(self) -> bool:
        return self.has_enough_sample and not self.is_market_maker


@dataclass(frozen=True, slots=True)
class MarketAggregate:
    """Hasil agregasi satu market resolve untuk satu wallet."""

    market_id: int
    staked: Decimal  # total USDC yang dibelanjakan (hanya buy)
    pnl: Decimal  # payout $1/token sisi menang dikurangi biaya bersih
    entry_usdc: Decimal  # USDC beli (untuk rata-rata berbobot)
    entry_tokens: Decimal  # token yang dibeli
    is_two_sided: bool  # memegang kedua sisi bersih sekaligus
    won: bool  # pnl market ini positif


def aggregate_market(market_id: int, trades: Sequence[Trade], winning: Outcome) -> MarketAggregate:
    """Agregasi posisi satu wallet pada satu market yang sudah resolve.

    PnL = token bersih sisi menang (tiap token membayar $1) - biaya bersih
    kedua sisi (beli - jual). Agregasi posisi ditulis lokal di sini;
    menyatukan dengan helper T-010 dicatat sebagai technical debt.
    """
    net_tokens = {Outcome.UP: _ZERO, Outcome.DOWN: _ZERO}
    net_cost = {Outcome.UP: _ZERO, Outcome.DOWN: _ZERO}
    staked = _ZERO
    entry_usdc = _ZERO
    entry_tokens = _ZERO

    for trade in trades:
        outcome = trade.outcome
        if trade.action.value == "buy":
            net_tokens[outcome] += trade.size
            net_cost[outcome] += trade.usdc
            staked += trade.usdc
            entry_usdc += trade.usdc
            entry_tokens += trade.size
        else:
            net_tokens[outcome] -= trade.size
            net_cost[outcome] -= trade.usdc

    payout = net_tokens[winning]  # 1 USDC per token sisi menang
    pnl = payout - net_cost[Outcome.UP] - net_cost[Outcome.DOWN]
    two_sided = net_tokens[Outcome.UP] > 0 and net_tokens[Outcome.DOWN] > 0

    return MarketAggregate(
        market_id=market_id,
        staked=staked,
        pnl=pnl,
        entry_usdc=entry_usdc,
        entry_tokens=entry_tokens,
        is_two_sided=two_sided,
        won=pnl > 0,
    )


def build_wallet_stats(
    wallet: str, aggregates: Sequence[MarketAggregate], policy: ScoringPolicy
) -> WalletStats:
    """Gabungkan agregat per market menjadi `WalletStats` + skor.

    Market maker: porsi market dua sisi ≥ `policy.market_maker_ratio`.
    """
    markets = len(aggregates)
    wins = sum(1 for agg in aggregates if agg.won)
    entry_usdc = sum((agg.entry_usdc for agg in aggregates), _ZERO)
    entry_tokens = sum((agg.entry_tokens for agg in aggregates), _ZERO)
    pnl = sum((agg.pnl for agg in aggregates), _ZERO)
    staked = sum((agg.staked for agg in aggregates), _ZERO)
    two_sided = sum(1 for agg in aggregates if agg.is_two_sided)

    avg_entry = entry_usdc / entry_tokens if entry_tokens > 0 else None
    is_market_maker = (
        markets > 0 and Decimal(two_sided) / Decimal(markets) >= policy.market_maker_ratio
    )

    stats = WalletStats(
        wallet=wallet,
        markets_count=markets,
        wins=wins,
        avg_entry=avg_entry,
        pnl=pnl,
        staked=staked,
        is_market_maker=is_market_maker,
    )
    return replace(stats, score=score_wallet(stats, policy))


def edge_shrunk(stats: WalletStats, policy: ScoringPolicy) -> Decimal:
    """Edge yang disusutkan: `edge * n / (n + k)`."""
    return _shrink(stats.edge, stats.markets_count, policy)


def roi_shrunk(stats: WalletStats, policy: ScoringPolicy) -> Decimal:
    """ROI yang disusutkan: `roi * n / (n + k)`."""
    return _shrink(stats.roi, stats.markets_count, policy)


def win_rate_confidence(stats: WalletStats, policy: ScoringPolicy | None = None) -> Decimal:
    """Keyakinan win rate (0-1): n / (n + k); tanpa policy pakai default."""
    k = policy.shrinkage_k if policy is not None else ScoringPolicy().shrinkage_k
    n = stats.markets_count
    if n <= 0:
        return _ZERO
    return Decimal(n) / (Decimal(n) + k)


def score_wallet(stats: WalletStats, policy: ScoringPolicy) -> Decimal:
    """Skor wallet 0-100 dari edge & ROI ter-shrink (ADR-003).

    Wallet tanpa sampel cukup → 0; market maker → 0; nilai di-clamp ke
    0-100 dan tidak pernah membagi nol (total bobot 0 → skor 0).
    """
    if stats.markets_count < policy.min_sample or stats.is_market_maker:
        return _ZERO

    total_weight = policy.edge_weight + policy.roi_weight
    if total_weight <= 0:
        return _ZERO

    edge_part = _clip(edge_shrunk(stats, policy) / _positive(policy.edge_scale))
    roi_part = _clip(roi_shrunk(stats, policy) / _positive(policy.roi_scale))
    raw = (policy.edge_weight * edge_part + policy.roi_weight * roi_part) / total_weight
    score = _clip(raw) * _HUNDRED
    return score if score <= _HUNDRED else _HUNDRED


def _shrink(value: Decimal, n: int, policy: ScoringPolicy) -> Decimal:
    if n <= 0 or policy.shrinkage_k < 0:
        return _ZERO
    return value * Decimal(n) / (Decimal(n) + policy.shrinkage_k)


def _clip(value: Decimal) -> Decimal:
    """Batasi ke rentang 0-1 (atau 0-100 bila input sudah persen)."""
    if value <= 0:
        return _ZERO
    return min(value, _ONE)


def _positive(scale: Decimal) -> Decimal:
    """Skala normalisasi tidak boleh nol/negatif; fallback ke 1."""
    return scale if scale > 0 else _ONE


__all__ = [
    "DEFAULT_MIN_SCORE",
    "MIN_SAMPLE_MARKETS",
    "MarketAggregate",
    "ScoringPolicy",
    "WalletStats",
    "aggregate_market",
    "build_wallet_stats",
    "edge_shrunk",
    "roi_shrunk",
    "score_wallet",
    "win_rate_confidence",
]
