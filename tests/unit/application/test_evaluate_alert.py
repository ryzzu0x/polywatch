"""Unit test T-017: evaluate_alert + formatter + manage_alerts (fake repo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.ports import (
    AlertSettingsRepository,
    AlertsSentRepository,
    MarketRepository,
    Notifier,
    UserRepository,
    WalletStatsRepository,
    WatchlistRepository,
)
from polywatch.application.use_cases.evaluate_alert import EvaluateAlert, format_alert
from polywatch.application.use_cases.manage_alerts import (
    InvalidAlertSettingError,
    ManageAlerts,
)
from polywatch.domain.market import Outcome
from polywatch.domain.scoring import WalletStats
from polywatch.domain.trade import Action, Trade

TELEGRAM_ID = 42
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
WALLET = "0x" + "ab" * 20
WALLET_MM = "0x" + "cd" * 20
WALLET_LOW = "0x" + "ef" * 20

MARKET = TokenMarketInfo(
    market_id=7,
    condition_id="0xcid",
    token_up="111",
    token_down="222",
    slug="btc-updown-5m-1000",
)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now_value = now

    def now(self) -> datetime:
        return self.now_value


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> None:
        self.sent.append(text)


class FakeUserRepo:
    async def get_id_by_telegram(self, telegram_id: int) -> int:
        return 1

    async def get_or_create_by_telegram(self, telegram_id: int) -> int:
        return 1


class FakeWalletStatsRepo:
    def __init__(self, rows: dict[str, WalletStats]) -> None:
        self.rows = rows

    async def get(self, wallet: str) -> WalletStats | None:
        return self.rows.get(wallet)


class FakeWatchlistRepo:
    def __init__(self, items: set[str] | None = None) -> None:
        self.items = set(items or [])

    async def is_watched(self, user_id: int, wallet: str) -> bool:
        return wallet in self.items


class FakeAlertSettingsRepo:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.saved: list[tuple[int, dict[str, object]]] = []
        self.values: dict[str, object] = values or {}

    async def get(self, user_id: int) -> dict[str, object]:
        return dict(self.values)

    async def save(self, user_id: int, values: dict[str, object]) -> None:
        self.saved.append((user_id, values))
        self.values.update(values)


class FakeAlertsSentRepo:
    def __init__(self, last: dict[str, datetime] | None = None) -> None:
        self.last_map: dict[str, datetime] = last or {}
        self.recorded: list[dict[str, object]] = []

    async def record(
        self,
        user_id: int,
        wallet: str,
        market_id: int,
        tx_hash: str,
        log_index: int,
        sent_at: datetime,
        delay_ms: int,
    ) -> None:
        self.recorded.append(
            {
                "user_id": user_id,
                "wallet": wallet,
                "market_id": market_id,
                "tx_hash": tx_hash,
                "log_index": log_index,
                "sent_at": sent_at,
                "delay_ms": delay_ms,
            }
        )
        self.last_map[wallet] = sent_at

    async def last_sent_at(self, user_id: int, wallet: str) -> datetime | None:
        return self.last_map.get(wallet)


class FakeMarketRepo:
    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return MARKET if market_id == MARKET.market_id else None


def _stats(wallet: str, score: str = "80", mm: bool = False) -> WalletStats:
    return WalletStats(
        wallet=wallet,
        markets_count=120,
        wins=80,
        avg_entry=Decimal("0.5"),
        pnl=Decimal("500"),
        staked=Decimal("1000"),
        is_market_maker=mm,
        score=Decimal(score),
    )


def _trade(
    wallet: str = WALLET,
    usdc: str = "500",
    action: Action = Action.BUY,
    outcome: Outcome = Outcome.UP,
    traded_at: datetime | None = None,
) -> Trade:
    return Trade(
        tx_hash="0xtx1",
        log_index=1,
        market_id=7,
        wallet=wallet,
        outcome=outcome,
        action=action,
        price=Decimal("0.60"),
        size=Decimal("833.33"),
        usdc=Decimal(usdc),
        traded_at=traded_at or NOW,
    )


def _make(
    stats_rows: dict[str, WalletStats] | None = None,
    watchlist: FakeWatchlistRepo | None = None,
    alert_settings: FakeAlertSettingsRepo | None = None,
    alerts_sent: FakeAlertsSentRepo | None = None,
    clock: FakeClock | None = None,
) -> tuple[EvaluateAlert, FakeNotifier]:
    notifier = FakeNotifier()
    evaluator = EvaluateAlert(
        wallet_stats=cast(WalletStatsRepository, FakeWalletStatsRepo(stats_rows or {})),
        markets=cast(MarketRepository, FakeMarketRepo()),
        users=cast(UserRepository, FakeUserRepo()),
        watchlist=cast(
            WatchlistRepository, watchlist if watchlist is not None else FakeWatchlistRepo()
        ),
        settings_repo=cast(
            AlertSettingsRepository,
            alert_settings if alert_settings is not None else FakeAlertSettingsRepo(),
        ),
        alerts_sent=cast(
            AlertsSentRepository, alerts_sent if alerts_sent is not None else FakeAlertsSentRepo()
        ),
        notifier=cast(Notifier, notifier),
        clock=clock or FakeClock(),
        telegram_id=TELEGRAM_ID,
    )
    return evaluator, notifier


# ---------------------------------------------------------------- terkirim


async def test_alert_terkirim_saat_lolos_semua_ambang() -> None:
    evaluator, notifier = _make({WALLET: _stats(WALLET, score="80")})
    decision = await evaluator.evaluate(_trade(usdc="500"))
    assert decision.sent
    assert decision.reason == "sent"
    assert len(notifier.sent) == 1


async def test_alert_tercatat_delay_ms_wajar() -> None:
    sent_repo = FakeAlertsSentRepo()
    traded_at = NOW - timedelta(seconds=2, milliseconds=500)
    evaluator, _ = _make({WALLET: _stats(WALLET)}, alerts_sent=sent_repo)
    await evaluator.evaluate(_trade(traded_at=traded_at))
    assert len(sent_repo.recorded) == 1
    record = sent_repo.recorded[0]
    assert record["delay_ms"] == 2500
    assert record["wallet"] == WALLET
    assert record["market_id"] == 7
    assert record["tx_hash"] == "0xtx1"


# ---------------------------------------------------------------- tidak terkirim


async def test_alert_tidak_terkirim_skor_rendah() -> None:
    evaluator, notifier = _make({WALLET: _stats(WALLET, score="40")})
    decision = await evaluator.evaluate(_trade(usdc="500"))
    assert not decision.sent
    assert decision.reason == "score_below_min"
    assert notifier.sent == []


async def test_alert_tidak_terkirim_market_maker() -> None:
    evaluator, notifier = _make({WALLET_MM: _stats(WALLET_MM, mm=True, score="90")})
    decision = await evaluator.evaluate(_trade(wallet=WALLET_MM, usdc="500"))
    assert not decision.sent
    assert decision.reason == "not_alertable"
    assert notifier.sent == []


async def test_alert_tidak_terkirim_sampel_kurang() -> None:
    stats = WalletStats(
        wallet=WALLET,
        markets_count=9,  # di bawah MIN_SAMPLE_MARKETS=10 (ADR-018)
        wins=8,
        avg_entry=Decimal("0.5"),
        pnl=Decimal("50"),
        staked=Decimal("100"),
        score=Decimal("90"),
    )
    evaluator, _ = _make({WALLET: stats})
    decision = await evaluator.evaluate(_trade())
    assert not decision.sent
    assert decision.reason == "not_alertable"


async def test_alert_tidak_terkirim_size_kecil() -> None:
    evaluator, notifier = _make({WALLET: _stats(WALLET)})
    decision = await evaluator.evaluate(_trade(usdc="10"))
    assert not decision.sent
    assert decision.reason == "usdc_below_min"
    assert notifier.sent == []


async def test_alert_tidak_terkirim_cooldown() -> None:
    alerts_sent = FakeAlertsSentRepo(last={WALLET: NOW - timedelta(seconds=60)})
    evaluator, notifier = _make({WALLET: _stats(WALLET)}, alerts_sent=alerts_sent)
    decision = await evaluator.evaluate(_trade(usdc="500"))
    assert not decision.sent
    assert decision.reason == "cooldown"
    assert notifier.sent == []
    # cooldown lewat → terkirim
    alerts_sent.last_map[WALLET] = NOW - timedelta(seconds=301)
    decision2 = await evaluator.evaluate(_trade(usdc="500"))
    assert decision2.sent


async def test_alert_tidak_terkirim_disabled() -> None:
    settings = FakeAlertSettingsRepo({"enabled": False})
    evaluator, _notifier = _make({WALLET: _stats(WALLET)}, alert_settings=settings)
    decision = await evaluator.evaluate(_trade(usdc="500"))
    assert not decision.sent
    assert decision.reason == "disabled"


# ---------------------------------------------------------------- watchlist prioritas


async def test_alert_watchlist_skor_rendah_tetap_terkirim_prioritas() -> None:
    watchlist = FakeWatchlistRepo({WALLET})
    evaluator, notifier = _make({WALLET: _stats(WALLET, score="40")}, watchlist=watchlist)
    decision = await evaluator.evaluate(_trade(usdc="500"))
    assert decision.sent
    assert notifier.sent
    assert notifier.sent[0].startswith("[WATCHLIST]")


# ---------------------------------------------------------------- formatter


def test_format_alert_isi_lengkap() -> None:
    stats = _stats(WALLET, score="80")
    text = format_alert(_trade(), stats, MARKET, watched=False)
    assert "Alert copy-trade" in text
    assert "btc-updown-5m-1000" in text
    assert "0xabab…abab" in text
    assert "skor 80" in text
    assert "edge 16.7%" in text
    assert "beli Up" in text
    assert "500 USDC" in text
    assert "@ 0.60" in text
    assert "https://polymarket.com/event/btc-updown-5m-1000" in text
    assert "[WATCHLIST]" not in text


def test_format_alert_jual_down_dan_watchlist() -> None:
    stats = _stats(WALLET)
    trade = _trade(action=Action.SELL, outcome=Outcome.DOWN)
    text = format_alert(trade, stats, MARKET, watched=True)
    assert text.startswith("[WATCHLIST]")
    assert "jual Down" in text


# ---------------------------------------------------------------- manage_alerts


async def test_manage_alerts_default() -> None:
    use_case = ManageAlerts(
        cast(UserRepository, FakeUserRepo()),
        cast(AlertSettingsRepository, FakeAlertSettingsRepo()),
    )
    values = await use_case.get(TELEGRAM_ID)
    assert values["min_score"] == Decimal("70")
    assert values["min_usdc"] == Decimal("100")
    assert values["cooldown_sec"] == 300
    assert values["enabled"] is True


async def test_manage_alerts_set_dan_format() -> None:
    repo = FakeAlertSettingsRepo()
    use_case = ManageAlerts(
        cast(UserRepository, FakeUserRepo()),
        cast(AlertSettingsRepository, repo),
    )
    await use_case.set_min_score(TELEGRAM_ID, Decimal("80"))
    await use_case.set_min_usdc(TELEGRAM_ID, Decimal("500"))
    await use_case.set_cooldown_sec(TELEGRAM_ID, 60)
    await use_case.set_enabled(TELEGRAM_ID, False)

    values = await use_case.get(TELEGRAM_ID)
    assert values["min_score"] == Decimal("80")
    assert values["min_usdc"] == Decimal("500")
    assert values["cooldown_sec"] == 60
    assert values["enabled"] is False

    text = ManageAlerts.format(values)
    assert "nonaktif" in text
    assert "Skor minimal: 80" in text
    assert "Size minimal: $500" in text
    assert "1 mnt" in text


async def test_manage_alerts_nilai_diluar_pilihan_ditolak() -> None:
    use_case = ManageAlerts(
        cast(UserRepository, FakeUserRepo()),
        cast(AlertSettingsRepository, FakeAlertSettingsRepo()),
    )
    with pytest.raises(InvalidAlertSettingError):
        await use_case.set_min_score(TELEGRAM_ID, Decimal("55"))
    with pytest.raises(InvalidAlertSettingError):
        await use_case.set_min_usdc(TELEGRAM_ID, Decimal("77"))
    with pytest.raises(InvalidAlertSettingError):
        await use_case.set_cooldown_sec(TELEGRAM_ID, 123)
