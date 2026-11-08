"""Use case evaluate_alert: keputusan & pengiriman alert copy-trade (T-017).

Dipanggil dari hook `on_saved` milik `ingest_trades` untuk tiap trade baru
yang tersimpan. Keputusan kirim:

1. `WalletStats` wallet wajib `is_alertable` (sampel cukup, bukan market
   maker) dan `score >= min_score`.
2. `trade.usdc >= min_usdc`.
3. Wallet di watchlist (bila watchlist tidak kosong) → tetap kirim dengan
   prioritas lebih tinggi (prefiks `[WATCHLIST]`).
4. Cooldown per (user, wallet): tidak kirim lebih sering dari
   `cooldown_sec` (default 300) sejak alert terakhir.
5. Alert dinonaktifkan (`enabled=false`) → tidak kirim.

Saat terkirim: catat ke `alerts_sent` termasuk `delay_ms` = selisih waktu
trade asli (`traded_at`) → waktu alert terkirim (`clock.now()`), lalu
kirim lewat port `Notifier`. Gagal kirim tidak menggagalkan ingest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.ports import (
    AlertSettingsRepository,
    AlertsSentRepository,
    Clock,
    MarketRepository,
    Notifier,
    UserRepository,
    WalletStatsRepository,
    WatchlistRepository,
)
from polywatch.application.use_cases.wallet_views import short_wallet
from polywatch.domain.market import Outcome
from polywatch.domain.scoring import WalletStats
from polywatch.domain.trade import Action, Trade

logger = logging.getLogger(__name__)

DEFAULT_MIN_SCORE = Decimal("70")
DEFAULT_MIN_USDC = Decimal("100")
DEFAULT_COOLDOWN_SEC = 300

#: Prefix alert untuk wallet yang ada di watchlist (prioritas lebih tinggi).
WATCHLIST_PREFIX = "[WATCHLIST] "

_POLYMARKET_EVENT_URL = "https://polymarket.com/event"


@dataclass(frozen=True, slots=True)
class AlertSettings:
    """Nilai terpakai (sudah dikonversi tipe) untuk satu evaluasi."""

    min_score: Decimal
    min_usdc: Decimal
    cooldown_sec: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """Hasil evaluasi satu trade (untuk log/test)."""

    sent: bool
    reason: str


@dataclass(slots=True)
class EvaluateAlert:
    """Evaluasi satu trade baru: layak kirim alert? Kirim bila ya."""

    wallet_stats: WalletStatsRepository
    markets: MarketRepository
    users: UserRepository
    watchlist: WatchlistRepository
    settings_repo: AlertSettingsRepository
    alerts_sent: AlertsSentRepository
    notifier: Notifier
    clock: Clock
    telegram_id: int

    async def evaluate(self, trade: Trade) -> AlertDecision:
        """Putuskan dan (bila layak) kirim alert untuk satu trade."""
        settings = await self._settings()
        if not settings.enabled:
            return AlertDecision(False, "disabled")

        stats = await self.wallet_stats.get(trade.wallet)
        if stats is None or not stats.is_alertable:
            return AlertDecision(False, "not_alertable")

        user_id = await self.users.get_or_create_by_telegram(self.telegram_id)
        watched = await self.watchlist.is_watched(user_id, trade.wallet)

        # Wallet di watchlist (jalur prioritas) tetap dikirim meski skor di
        # bawah ambang; wallet biasa wajib lolos ambang skor.
        if not watched and stats.score < settings.min_score:
            return AlertDecision(False, "score_below_min")
        if trade.usdc < settings.min_usdc:
            return AlertDecision(False, "usdc_below_min")

        now = self.clock.now()
        last = await self.alerts_sent.last_sent_at(user_id, trade.wallet)
        cooldown_sec = settings.cooldown_sec
        if last is not None and (now - last).total_seconds() < cooldown_sec:
            return AlertDecision(False, "cooldown")

        market = await self.markets.get_token_market(trade.market_id)
        if market is None:
            return AlertDecision(False, "market_not_found")

        delay_ms = max(0, round((now - trade.traded_at).total_seconds() * 1000))
        await self.alerts_sent.record(
            user_id=user_id,
            wallet=trade.wallet,
            market_id=trade.market_id,
            tx_hash=trade.tx_hash,
            log_index=trade.log_index,
            sent_at=now,
            delay_ms=delay_ms,
        )
        await self.notifier.send(
            format_alert(
                trade=trade,
                stats=stats,
                market=market,
                watched=watched,
            )
        )
        logger.info(
            "alert terkirim",
            extra={
                "event": "alert_sent",
                "wallet": trade.wallet,
                "market_id": trade.market_id,
                "score": str(stats.score),
                "usdc": str(trade.usdc),
                "delay_ms": delay_ms,
                "watched": watched,
            },
        )
        return AlertDecision(True, "sent")

    async def _settings(self) -> AlertSettings:
        user_id = await self.users.get_or_create_by_telegram(self.telegram_id)
        values = await self.settings_repo.get(user_id)
        return AlertSettings(
            min_score=_decimal_or(values.get("min_score"), DEFAULT_MIN_SCORE),
            min_usdc=_decimal_or(values.get("min_usdc"), DEFAULT_MIN_USDC),
            cooldown_sec=_int_or(values.get("cooldown_sec"), DEFAULT_COOLDOWN_SEC),
            enabled=bool(values.get("enabled", True)),
        )


def _decimal_or(raw: object, default: Decimal) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _int_or(raw: object, default: int) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def format_alert(
    trade: Trade,
    stats: WalletStats,
    market: TokenMarketInfo,
    watched: bool,
) -> str:
    """Formatter murni pesan alert (Bahasa Indonesia, ringkas).

    Berisi: aset/timeframe, wallet pendek, aksi beli/jual, outcome Up/Down,
    size USDC, harga, skor & edge wallet, dan link market.
    """
    outcome = "Up" if trade.outcome is Outcome.UP else "Down"
    action = "beli" if trade.action is Action.BUY else "jual"
    edge_pct = f"{stats.edge * 100:.1f}%"
    link = f"{_POLYMARKET_EVENT_URL}/{market.slug}"
    prefix = WATCHLIST_PREFIX if watched else ""
    return (
        f"{prefix}Alert copy-trade\n"
        f"Market: {market.slug}\n"
        f"Wallet: {short_wallet(trade.wallet)} (skor {stats.score:.0f}, edge {edge_pct})\n"
        f"Aksi: {action} {outcome} — {trade.usdc} USDC @ {trade.price}\n"
        f"Link: {link}"
    )


__all__ = [
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_MIN_USDC",
    "AlertDecision",
    "AlertSettings",
    "EvaluateAlert",
    "format_alert",
]
