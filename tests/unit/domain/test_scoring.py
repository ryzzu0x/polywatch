"""Unit test skor wallet (ADR-003)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from polywatch.domain.market import Outcome
from polywatch.domain.scoring import (
    MIN_SAMPLE_MARKETS,
    MarketAggregate,
    ScoringPolicy,
    WalletStats,
    aggregate_market,
    build_wallet_stats,
    edge_shrunk,
    roi_shrunk,
    score_wallet,
    win_rate_confidence,
)
from polywatch.domain.trade import Action, Trade

_TS = datetime(2026, 11, 8, tzinfo=UTC)


def _stats(**overrides: object) -> WalletStats:
    values: dict[str, object] = {
        "wallet": "0xabc",
        "markets_count": 100,
        "wins": 85,
        "avg_entry": Decimal("0.90"),
        "pnl": Decimal("-500"),
        "staked": Decimal("10000"),
    }
    values.update(overrides)
    return WalletStats(**values)  # type: ignore[arg-type]


def _trade(
    market_id: int = 1,
    outcome: Outcome = Outcome.UP,
    action: Action = Action.BUY,
    price: str = "0.5",
    size: str = "100",
    usdc: str | None = None,
    tx: int = 1,
) -> Trade:
    price_d = Decimal(price)
    size_d = Decimal(size)
    usdc_d = Decimal(usdc) if usdc is not None else price_d * size_d
    return Trade(
        tx_hash=f"0x{tx:064x}",
        log_index=tx,
        market_id=market_id,
        wallet="0x" + "ab" * 20,
        outcome=outcome,
        action=action,
        price=price_d,
        size=size_d,
        usdc=usdc_d,
        traded_at=_TS,
    )


def test_menang_85_persen_dengan_entry_090_punya_edge_negatif() -> None:
    stats = _stats()

    assert stats.win_rate == Decimal("0.85")
    assert stats.edge == Decimal("-0.05")
    assert stats.roi == Decimal("-0.05")


def test_edge_positif_untuk_wallet_yang_menang_di_atas_harga_masuk() -> None:
    stats = _stats(wins=60, avg_entry=Decimal("0.50"))

    assert stats.edge == Decimal("0.10")
    assert stats.roi == Decimal("-0.05")


def test_sampel_kecil_belum_layak_dapat_alert() -> None:
    stats = _stats(markets_count=MIN_SAMPLE_MARKETS - 1)

    assert stats.has_enough_sample is False
    assert stats.is_alertable is False


def test_min_sample_default_10_adr018() -> None:
    """ADR-018: default min_sample = 10 untuk MVP (0 wallet mencapai 50);
    wallet dengan tepat 10 market resolve sudah layak alert, 9 belum."""
    assert MIN_SAMPLE_MARKETS == 10
    assert ScoringPolicy().min_sample == 10
    assert _stats(markets_count=10).has_enough_sample is True
    assert _stats(markets_count=9).has_enough_sample is False


def test_shrinkage_k_tetap_50_adr018() -> None:
    """ADR-018: shrinkage_k TETAP 50 — penyusutan meredam alert lebih berisik."""
    policy = ScoringPolicy()
    assert policy.shrinkage_k == Decimal(50)
    # edge 20%: n=10 → 20*10/60 ≈ 3,3%; n=100 → 20*100/150 ≈ 13,3%
    stats = _stats(markets_count=10, wins=10, avg_entry=Decimal("0.80"))
    assert edge_shrunk(stats, policy) == Decimal("0.20") * Decimal(10) / Decimal(60)


def test_market_maker_tidak_layak_dapat_alert() -> None:
    stats = _stats(markets_count=MIN_SAMPLE_MARKETS, is_market_maker=True)

    assert stats.has_enough_sample is True
    assert stats.is_alertable is False


def test_wallet_tanpa_data_tidak_membagi_nol() -> None:
    stats = WalletStats(
        wallet="0xabc",
        markets_count=0,
        wins=0,
        avg_entry=None,
        pnl=Decimal(0),
        staked=Decimal(0),
    )

    assert stats.win_rate == Decimal(0)
    assert stats.edge == Decimal(0)
    assert stats.roi == Decimal(0)
    assert score_wallet(stats, ScoringPolicy()) == Decimal(0)


# --- T-012: ScoringPolicy, shrinkage, skor 0-100 --------------------------


def test_beli_090_menang_85_persen_skor_rendah() -> None:
    """Kasus TASKS: win rate tinggi tapi entry mahal → skor 0 (edge negatif)."""
    policy = ScoringPolicy(min_sample=50)
    stats = _stats()  # edge -0.05, roi -0.05, 100 market

    score = score_wallet(stats, policy)

    assert score == Decimal(0)


def test_edge_positif_sampel_besar_skor_tinggi() -> None:
    policy = ScoringPolicy()
    stats = _stats(
        wins=75, avg_entry=Decimal("0.50"), pnl=Decimal("2500"), staked=Decimal("10000")
    )  # edge 0.25, roi 0.25

    score = score_wallet(stats, policy)

    assert score > Decimal(50)


def test_edge_ter_shrink_persamaan_n_over_n_plus_k() -> None:
    policy = ScoringPolicy(shrinkage_k=Decimal(50))
    stats = _stats(wins=75, avg_entry=Decimal("0.50"))  # edge 0.25, n=100

    assert edge_shrunk(stats, policy) == Decimal("0.25") * Decimal(100) / Decimal(150)


def test_roi_ter_shrink_persamaan_n_over_n_plus_k() -> None:
    policy = ScoringPolicy(shrinkage_k=Decimal(50))
    stats = _stats(wins=75, avg_entry=Decimal("0.50"), pnl=Decimal("2500"))

    assert roi_shrunk(stats, policy) == Decimal("0.25") * Decimal(100) / Decimal(150)


def test_sampel_kecil_tersusutkan_tidak_melampaui_sampel_besar() -> None:
    """Edge & ROI sama, n=60 < n=120 → skor lebih kecil."""
    policy = ScoringPolicy(min_sample=50)
    big = _stats(
        markets_count=120,
        wins=90,
        avg_entry=Decimal("0.50"),
        pnl=Decimal("3000"),
        staked=Decimal("12000"),
    )  # edge/roi 0.25
    small = _stats(
        markets_count=60,
        wins=45,
        avg_entry=Decimal("0.50"),
        pnl=Decimal("1500"),
        staked=Decimal("6000"),
    )  # edge/roi 0.25

    assert score_wallet(small, policy) < score_wallet(big, policy)
    assert score_wallet(small, policy) > Decimal(0)


def test_sampel_di_bawah_minimum_skor_nol() -> None:
    policy = ScoringPolicy(min_sample=50)
    stats = _stats(
        markets_count=49,
        wins=40,
        avg_entry=Decimal("0.50"),
        pnl=Decimal("1000"),
        staked=Decimal("4000"),
    )

    assert score_wallet(stats, policy) == Decimal(0)


def test_market_maker_skor_nol_meski_performa_baik() -> None:
    policy = ScoringPolicy()
    stats = _stats(wins=75, avg_entry=Decimal("0.50"), pnl=Decimal("2500"), is_market_maker=True)

    assert score_wallet(stats, policy) == Decimal(0)


def test_skor_dibatasi_0_sampai_100() -> None:
    policy = ScoringPolicy(edge_scale=Decimal("0.01"), roi_scale=Decimal("0.01"))
    stats = _stats(wins=95, avg_entry=Decimal("0.40"), pnl=Decimal("9000"))

    assert score_wallet(stats, policy) == Decimal(100)


def test_policy_bobot_nol_tidak_membagi_nol() -> None:
    policy = ScoringPolicy(edge_weight=Decimal(0), roi_weight=Decimal(0))
    stats = _stats(wins=75, avg_entry=Decimal("0.50"), pnl=Decimal("2500"))

    assert score_wallet(stats, policy) == Decimal(0)


def test_policy_skala_nol_ditangani() -> None:
    policy = ScoringPolicy(edge_scale=Decimal(0), roi_scale=Decimal(0))
    stats = _stats(wins=75, avg_entry=Decimal("0.50"), pnl=Decimal("2500"))

    assert score_wallet(stats, policy) > Decimal(0)


def test_win_rate_confidence_naik_dengan_sampel() -> None:
    policy = ScoringPolicy(shrinkage_k=Decimal(50))
    small = _stats(markets_count=10)
    big = _stats(markets_count=200)

    assert win_rate_confidence(small, policy) < win_rate_confidence(big, policy)
    assert win_rate_confidence(WalletStats("0xabc", 0, 0, None, Decimal(0), Decimal(0))) == Decimal(
        0
    )


# --- T-012: agregasi posisi per market ------------------------------------


def test_aggregate_market_beli_sisi_menang() -> None:
    trades = [_trade(outcome=Outcome.UP, price="0.50", size="100", usdc="50")]

    agg = aggregate_market(1, trades, winning=Outcome.UP)

    assert agg.pnl == Decimal("50")  # payout 100 - biaya 50
    assert agg.staked == Decimal("50")
    assert agg.entry_usdc == Decimal("50")
    assert agg.entry_tokens == Decimal("100")
    assert agg.is_two_sided is False
    assert agg.won is True


def test_aggregate_market_beli_sisi_kalah() -> None:
    trades = [_trade(outcome=Outcome.UP, price="0.50", size="100", usdc="50")]

    agg = aggregate_market(1, trades, winning=Outcome.DOWN)

    assert agg.pnl == Decimal("-50")
    assert agg.won is False


def test_aggregate_market_dua_sisi_terdeteksi() -> None:
    trades = [
        _trade(outcome=Outcome.UP, price="0.50", size="100", usdc="50", tx=1),
        _trade(outcome=Outcome.DOWN, price="0.50", size="100", usdc="50", tx=2),
    ]

    agg = aggregate_market(1, trades, winning=Outcome.UP)

    assert agg.is_two_sided is True
    assert agg.pnl == Decimal(0)  # beli kedua sisi 0.50/0.50: payout 100 = biaya 100
    assert agg.staked == Decimal(100)


def test_aggregate_market_jual_mengurangi_posisi_bersih() -> None:
    trades = [
        _trade(action=Action.BUY, outcome=Outcome.UP, price="0.50", size="100", usdc="50", tx=1),
        _trade(action=Action.SELL, outcome=Outcome.UP, price="0.60", size="40", usdc="24", tx=2),
    ]

    agg = aggregate_market(1, trades, winning=Outcome.UP)

    assert agg.pnl == Decimal("60") - Decimal("26")  # payout 60, biaya bersih 26
    assert agg.staked == Decimal("50")  # hanya buy dihitung staked
    assert agg.entry_usdc == Decimal("50")
    assert agg.entry_tokens == Decimal("100")


def test_aggregate_market_tanpa_trade() -> None:
    agg = aggregate_market(1, [], winning=Outcome.UP)

    assert agg.pnl == Decimal(0)
    assert agg.staked == Decimal(0)
    assert agg.entry_tokens == Decimal(0)
    assert agg.is_two_sided is False
    assert agg.won is False


def test_build_wallet_stats_rata_rata_berbobot_dan_flag_market_maker() -> None:
    # 10 market dua sisi dari 30 → rasio 1/3 ≥ 0.30 → market maker
    aggs: list[MarketAggregate] = []
    for i in range(30):
        two_sided = i < 10
        aggs.append(
            MarketAggregate(
                market_id=i,
                staked=Decimal("100"),
                pnl=Decimal("20") if i % 2 == 0 else Decimal("-30"),
                entry_usdc=Decimal("50"),
                entry_tokens=Decimal("100"),
                is_two_sided=two_sided,
                won=i % 2 == 0,
            )
        )

    stats = build_wallet_stats("0x" + "ab" * 20, aggs, ScoringPolicy(min_sample=20))

    assert stats.is_market_maker is True
    assert stats.markets_count == 30
    assert stats.wins == 15
    assert stats.avg_entry == Decimal("0.50")
    assert stats.pnl == Decimal("15") * Decimal("20") + Decimal("15") * Decimal("-30")
    assert stats.score == Decimal(0)  # market maker → 0


def test_build_wallet_stats_tidak_market_maker_bila_rasio_rendah() -> None:
    aggs = [
        MarketAggregate(
            market_id=i,
            staked=Decimal("100"),
            pnl=Decimal("20"),
            entry_usdc=Decimal("50"),
            entry_tokens=Decimal("100"),
            is_two_sided=i < 8,  # 8/30 ≈ 0.27 < 0.30
            won=True,
        )
        for i in range(30)
    ]

    stats = build_wallet_stats("0x" + "ab" * 20, aggs, ScoringPolicy(min_sample=20))

    assert stats.is_market_maker is False
    assert stats.score > Decimal(0)


def test_build_wallet_stats_tanpa_history() -> None:
    stats = build_wallet_stats("0x" + "ab" * 20, [], ScoringPolicy())

    assert stats.markets_count == 0
    assert stats.avg_entry is None
    assert stats.pnl == Decimal(0)
    assert stats.staked == Decimal(0)
    assert stats.is_market_maker is False
    assert stats.score == Decimal(0)
