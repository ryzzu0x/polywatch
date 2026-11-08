"""Unit test agregasi Trade → Position.

Menguji apply_trade, positions_from_trades, realize, average_entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polywatch.domain.errors import OversellError
from polywatch.domain.market import Outcome
from polywatch.domain.trade import (
    Action,
    Position,
    Trade,
    apply_trade,
    average_entry,
    normalize_wallet,
    positions_from_trades,
    realize,
)

WALLET = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"
OTHER_WALLET = "0x0000000000000000000000000000000000000099"


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


def _empty() -> Position:
    return Position(wallet=normalize_wallet(WALLET), market_id=10)


# --- apply_trade ---------------------------------------------------------


def test_buy_menambah_net_dan_cost() -> None:
    pos = apply_trade(_empty(), _trade())
    assert pos.net_up == Decimal("100")
    assert pos.cost_up == Decimal("55")
    assert pos.net_down == 0
    assert pos.cost_down == 0


def test_buy_sisi_down() -> None:
    pos = apply_trade(
        _empty(),
        _trade(outcome=Outcome.DOWN, price=Decimal("0.4"), usdc=Decimal("40")),
    )
    assert pos.net_down == Decimal("100")
    assert pos.cost_down == Decimal("40")
    assert pos.net_up == 0


def test_sell_mengurangi_net_tanpa_mengubah_cost() -> None:
    pos = apply_trade(_empty(), _trade())
    pos = apply_trade(pos, _trade(action=Action.SELL, size=Decimal("40"), usdc=Decimal("22")))
    assert pos.net_up == Decimal("60")
    assert pos.cost_up == Decimal("55")  # biaya total tetap, dasar PnL


def test_sell_habiskan_posisi() -> None:
    pos = apply_trade(_empty(), _trade())
    pos = apply_trade(pos, _trade(action=Action.SELL, size=Decimal("100"), usdc=Decimal("60")))
    assert pos.net_up == 0
    assert pos.cost_up == Decimal("55")


def test_sell_melebihi_posisi_ditolak() -> None:
    pos = apply_trade(_empty(), _trade())
    with pytest.raises(OversellError):
        apply_trade(pos, _trade(action=Action.SELL, size=Decimal("100.000001"), usdc=Decimal("60")))


def test_sell_tanpa_posisi_ditolak() -> None:
    with pytest.raises(OversellError):
        apply_trade(_empty(), _trade(action=Action.SELL))


def test_sell_sisi_berbeda_tidak_tercampur() -> None:
    pos = apply_trade(_empty(), _trade())
    with pytest.raises(OversellError):
        apply_trade(pos, _trade(action=Action.SELL, outcome=Outcome.DOWN, price=Decimal("0.5")))


def test_trade_tidak_cocok_ditolak() -> None:
    with pytest.raises(ValueError, match="tidak cocok"):
        apply_trade(_empty(), _trade(wallet=OTHER_WALLET))
    with pytest.raises(ValueError, match="tidak cocok"):
        apply_trade(_empty(), _trade(market_id=99))


# --- positions_from_trades ----------------------------------------------


def test_agregasi_beli_bertahap_dua_sisi() -> None:
    trades = [
        _trade(log_index=1, size=Decimal("10"), usdc=Decimal("6")),
        _trade(log_index=2, price=Decimal("0.7"), size=Decimal("10"), usdc=Decimal("7")),
        _trade(
            log_index=3,
            outcome=Outcome.DOWN,
            price=Decimal("0.3"),
            size=Decimal("5"),
            usdc=Decimal("1.5"),
        ),
    ]
    positions = positions_from_trades(trades)
    pos = positions[(WALLET.lower(), 10)]
    assert len(positions) == 1
    assert pos.net_up == Decimal("20")
    assert pos.cost_up == Decimal("13")
    assert pos.net_down == Decimal("5")
    assert pos.cost_down == Decimal("1.5")
    assert pos.is_two_sided


def test_agregasi_terpisah_per_wallet_dan_market() -> None:
    trades = [
        _trade(market_id=10),
        _trade(market_id=11, tx_hash="0xbeef"),
        _trade(wallet=OTHER_WALLET, market_id=10, tx_hash="0xcafe"),
    ]
    positions = positions_from_trades(trades)
    assert set(positions) == {
        (WALLET.lower(), 10),
        (WALLET.lower(), 11),
        (OTHER_WALLET, 10),
    }


def test_jual_parsial_setelah_beli_bertahap() -> None:
    trades = [
        _trade(log_index=1, price=Decimal("0.6"), size=Decimal("10"), usdc=Decimal("6")),
        _trade(log_index=2, price=Decimal("0.7"), size=Decimal("10"), usdc=Decimal("7")),
        _trade(log_index=3, action=Action.SELL, size=Decimal("5"), usdc=Decimal("3.5")),
    ]
    pos = positions_from_trades(trades)[(WALLET.lower(), 10)]
    assert pos.net_up == Decimal("15")
    assert pos.cost_up == Decimal("13")


def test_daftar_kosong_menghasilkan_kosong() -> None:
    assert positions_from_trades([]) == {}


# --- realize ------------------------------------------------------------


def test_realize_menang_up() -> None:
    pos = apply_trade(_empty(), _trade())  # 100 UP, cost 55
    assert realize(pos, Outcome.UP) == Decimal("45")


def test_realize_kalah_down() -> None:
    pos = apply_trade(_empty(), _trade())
    assert realize(pos, Outcome.DOWN) == Decimal("-55")


def test_realize_dua_sisi_market_maker() -> None:
    pos = _empty()
    pos = apply_trade(pos, _trade())  # 100 UP, cost 55
    pos = apply_trade(
        pos,
        _trade(
            log_index=2,
            outcome=Outcome.DOWN,
            price=Decimal("0.4"),
            size=Decimal("100"),
            usdc=Decimal("40"),
        ),
    )
    # menang UP: 100 - (55 + 40) = 5; menang DOWN: 100 - 95 = 5
    assert realize(pos, Outcome.UP) == Decimal("5")
    assert realize(pos, Outcome.DOWN) == Decimal("5")


def test_realize_posisi_kosong() -> None:
    assert realize(_empty(), Outcome.UP) == 0


def test_realize_setelah_jual_penuh() -> None:
    pos = apply_trade(_empty(), _trade())
    pos = apply_trade(pos, _trade(action=Action.SELL, size=Decimal("100"), usdc=Decimal("70")))
    assert realize(pos, Outcome.UP) == Decimal("-55")
    assert realize(pos, Outcome.DOWN) == Decimal("-55")


# --- average_entry ------------------------------------------------------


def test_average_entry_berbobot_size() -> None:
    pos = _empty()
    pos = apply_trade(pos, _trade(price=Decimal("0.6"), size=Decimal("10"), usdc=Decimal("6")))
    pos = apply_trade(pos, _trade(price=Decimal("0.7"), size=Decimal("30"), usdc=Decimal("21")))
    # (6 + 21) / 40 = 0.675, bukan rata-rata sederhana 0.65
    assert average_entry(pos) == Decimal("0.675")


def test_average_entry_sisi_down() -> None:
    pos = apply_trade(
        _empty(),
        _trade(outcome=Outcome.DOWN, price=Decimal("0.4"), size=Decimal("10"), usdc=Decimal("4")),
    )
    assert average_entry(pos) == Decimal("0.4")


def test_average_entry_posisi_kosong_none() -> None:
    assert average_entry(_empty()) is None
    pos = apply_trade(_empty(), _trade())
    pos = apply_trade(pos, _trade(action=Action.SELL, size=Decimal("100"), usdc=Decimal("60")))
    assert average_entry(pos) is None


def test_average_entry_dua_sisi_pakai_net_terbesar() -> None:
    pos = _empty()
    pos = apply_trade(pos, _trade(size=Decimal("10"), usdc=Decimal("6")))
    pos = apply_trade(
        pos,
        _trade(
            log_index=2,
            outcome=Outcome.DOWN,
            price=Decimal("0.5"),
            size=Decimal("30"),
            usdc=Decimal("15"),
        ),
    )
    assert average_entry(pos) == Decimal("0.5")


def test_presisi_decimal_dijaga() -> None:
    pos = apply_trade(
        _empty(),
        _trade(price=Decimal("0.333333"), size=Decimal("3"), usdc=Decimal("0.999999")),
    )
    assert average_entry(pos) == Decimal("0.333333")
    assert realize(pos, Outcome.UP) == Decimal("3") - Decimal("0.999999")
