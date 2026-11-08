"""Unit test trade dan posisi."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polywatch.domain.errors import InvalidWalletError
from polywatch.domain.market import Outcome
from polywatch.domain.trade import Action, Position, Trade, normalize_wallet

WALLET = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"


def _trade(**overrides: object) -> Trade:
    values: dict[str, object] = {
        "tx_hash": "0xdead",
        "log_index": 1,
        "market_id": 10,
        "wallet": WALLET,
        "outcome": Outcome.UP,
        "action": Action.BUY,
        "price": Decimal("0.55"),
        "size": Decimal("100"),
        "usdc": Decimal("55"),
        "traded_at": datetime(2026, 11, 8, tzinfo=UTC),
    }
    values.update(overrides)
    return Trade(**values)  # type: ignore[arg-type]


def test_normalize_wallet_lowercase() -> None:
    assert normalize_wallet(WALLET) == WALLET.lower()


@pytest.mark.parametrize("raw", ["", "0x123", "bc1qxyz", "0x" + "z" * 40])
def test_normalize_wallet_tidak_valid(raw: str) -> None:
    with pytest.raises(InvalidWalletError):
        normalize_wallet(raw)


def test_trade_menormalkan_wallet() -> None:
    assert _trade().wallet == WALLET.lower()


@pytest.mark.parametrize("price", ["1.5", "-0.1"])
def test_trade_menolak_harga_di_luar_rentang(price: str) -> None:
    with pytest.raises(ValueError, match="harga"):
        _trade(price=Decimal(price))


def test_trade_menolak_nilai_negatif() -> None:
    with pytest.raises(ValueError, match="negatif"):
        _trade(size=Decimal("-1"))


def test_position_dua_sisi_menandai_market_maker() -> None:
    assert Position(wallet="0xabc", market_id=1, net_up=Decimal(5)).is_two_sided is False
    assert (
        Position(
            wallet="0xabc",
            market_id=1,
            net_up=Decimal(5),
            net_down=Decimal(5),
        ).is_two_sided
        is True
    )
