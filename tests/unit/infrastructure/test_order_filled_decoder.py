"""Unit test decoder OrderFilled CTF Exchange V2 — fixture receipt NYATA.

Fixture `tests/fixtures/t005c_receipt_orderfilled.json` diambil dari tx
real `0x7c55f9c66f...` (spike T-005c, market sol-updown-15m) via
`eth_getTransactionReceipt` — bukan data karangan.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from polywatch.infrastructure.polymarket.order_filled_decoder import (
    ORDER_FILLED_TOPIC,
    OrderFilledDecodeError,
    decode_order_filled,
    order_filled_logs,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "t005c_receipt_orderfilled.json"


@pytest.fixture(scope="module")
def receipt() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_fixture_berisi_order_filled(receipt: dict[str, Any]) -> None:
    logs: list[dict[str, Any]] = order_filled_logs(receipt)
    assert len(logs) == 2
    topics = [log["topics"] for log in logs]
    assert all(t[0] == ORDER_FILLED_TOPIC for t in topics)


def test_decode_log_pertama_maker_taker_jumlah(receipt: dict[str, Any]) -> None:
    log = order_filled_logs(receipt)[0]
    filled = decode_order_filled(log)
    # nilai dari fixture nyata (tx 0x7c55f9c66f..., market sol-updown-15m)
    assert filled.maker == "0xd6d2b81f0c60e358500a84ce8fbd566bbcc73f40"
    assert filled.taker == "0x59178ed0f66bcfa02193bcc77097715399bedb33"
    assert filled.side == 1  # SELL
    assert filled.size == Decimal("8.333334")
    assert filled.usdc == Decimal("1")
    assert filled.price == Decimal("1") / Decimal("8.333334")
    assert filled.fee == 0
    assert filled.log_index == 850
    assert filled.order_hash.startswith("0x")
    assert len(filled.order_hash) == 66
    # token id = asset_id event WS pada fixture (validasi silang)
    assert str(filled.token_id) == str(receipt["ws_event"]["asset_id"])


def test_decode_log_kedua_buy_facing_exchange(receipt: dict[str, object]) -> None:
    filled = decode_order_filled(order_filled_logs(receipt)[1])
    assert filled.maker == "0x59178ed0f66bcfa02193bcc77097715399bedb33"
    assert filled.taker == "0xe111180000d2663c0091e4f400237545b87b996b"  # exchange
    assert filled.side == 0  # BUY
    assert filled.size == Decimal("8.333334")
    assert filled.usdc == Decimal("1")
    assert filled.fee == 61600
    assert filled.log_index == 852


def test_decode_topik_salah_ditolak() -> None:
    log: dict[str, object] = {
        "topics": ["0x" + "00" * 32, "0x" + "11" * 32, "0x" + "22" * 32, "0x" + "33" * 32],
        "data": "0x" + "00" * 224,
        "logIndex": "0x1",
    }
    with pytest.raises(OrderFilledDecodeError, match="bukan OrderFilled"):
        decode_order_filled(log)


def test_decode_data_terlalu_pendek_ditolak() -> None:
    log: dict[str, object] = {
        "topics": [ORDER_FILLED_TOPIC, "0x" + "11" * 32, "0x" + "22" * 32, "0x" + "33" * 32],
        "data": "0x" + "00" * 64,
        "logIndex": "0x1",
    }
    with pytest.raises(OrderFilledDecodeError, match="panjang data"):
        decode_order_filled(log)


def test_decode_topik_tidak_lengkap_ditolak() -> None:
    log: dict[str, object] = {
        "topics": [ORDER_FILLED_TOPIC],
        "data": "0x" + "00" * 224,
        "logIndex": "0x1",
    }
    with pytest.raises(OrderFilledDecodeError, match="topik"):
        decode_order_filled(log)


def test_order_filled_logs_abu_abu_untuk_receipt_lain() -> None:
    assert order_filled_logs({"logs": [{"topics": ["0xabc"]}, {"topics": []}, "bukan"]}) == []
    assert order_filled_logs({}) == []
