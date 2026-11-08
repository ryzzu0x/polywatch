"""Unit test skor wallet (ADR-003)."""

from __future__ import annotations

from decimal import Decimal

from polywatch.domain.scoring import MIN_SAMPLE_MARKETS, WalletStats


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


def test_market_maker_tidak_layak_dapat_alert() -> None:
    stats = _stats(is_market_maker=True)

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
