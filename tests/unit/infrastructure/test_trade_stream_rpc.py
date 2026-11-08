"""Unit test parse frame WS + adapter stream + klien RPC failover (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from polywatch.infrastructure.polymarket.orderbook_ws import parse_last_trade_events
from polywatch.infrastructure.polymarket.polygon_rpc import (
    PolygonRpcClient,
    RpcUnavailableError,
)

RECEIPT = {"jsonrpc": "2.0", "id": 1, "result": {"logs": [], "status": "0x1"}}


def test_parse_frame_objek_tunggal() -> None:
    frame = (
        '{"event_type":"last_trade_price","asset_id":"123","price":"0.5",'
        '"size":"10","side":"BUY","timestamp":"1794122884052",'
        '"market":"0xabc","transaction_hash":"0xdead"}'
    )
    events = parse_last_trade_events(frame)
    assert len(events) == 1
    assert events[0].tx_hash == "0xdead"
    assert events[0].timestamp_ms == 1794122884052
    assert events[0].condition_id == "0xabc"


def test_parse_frame_list_dengan_event_lain() -> None:
    frame = (
        '[{"event_type":"price_change","price_changes":[]},'
        '{"event_type":"last_trade_price","asset_id":"1","transaction_hash":"0x1",'
        '"timestamp":"100"},'
        '{"event_type":"book"}]'
    )
    events = parse_last_trade_events(frame)
    assert len(events) == 1
    assert events[0].tx_hash == "0x1"


def test_parse_frame_tanpa_hash_diabaikan() -> None:
    frame = '[{"event_type":"last_trade_price","asset_id":"1"}]'
    assert parse_last_trade_events(frame) == []


def test_parse_frame_rusak_raise() -> None:
    with pytest.raises(ValueError, match="Expecting"):
        parse_last_trade_events("{bukan json")


@respx.mock
async def test_rpc_receipt_sukses_endpoint_pertama() -> None:
    respx.post("https://a.example/rpc").respond(json=RECEIPT)
    async with PolygonRpcClient(endpoints=["https://a.example/rpc"]) as client:
        result = await client.get_transaction_receipt("0x1")
    assert result is not None
    assert result["status"] == "0x1"


@respx.mock
async def test_rpc_failover_rotasi_ke_endpoint_berikutnya() -> None:
    bad = respx.post("https://bad.example/rpc").respond(status_code=500)
    good = respx.post("https://good.example/rpc").respond(json=RECEIPT)
    async with PolygonRpcClient(
        endpoints=["https://bad.example/rpc", "https://good.example/rpc"]
    ) as client:
        result = await client.get_transaction_receipt("0x1")
    assert result is not None
    assert bad.called
    assert good.called


@respx.mock
async def test_rpc_json_error_memicu_failover() -> None:
    bad = respx.post("https://bad.example/rpc").respond(json={"error": {"code": -32601}})
    good = respx.post("https://good.example/rpc").respond(json=RECEIPT)
    async with PolygonRpcClient(
        endpoints=["https://bad.example/rpc", "https://good.example/rpc"]
    ) as client:
        await client.get_transaction_receipt("0x1")
    assert bad.called
    assert good.called


@respx.mock
async def test_rpc_semua_gagal_raise_unavailable() -> None:
    respx.post("https://bad.example/rpc").respond(status_code=500)
    respx.post("https://worse.example/rpc").respond(status_code=503)
    async with PolygonRpcClient(
        endpoints=["https://bad.example/rpc", "https://worse.example/rpc"]
    ) as client:
        with pytest.raises(RpcUnavailableError):
            await client.get_transaction_receipt("0x1")


@respx.mock
async def test_rpc_receipt_null_dipoll_sampai_hadir() -> None:
    """Event WS pra-konfirmasi: receipt null dulu, hadir di poll kedua."""
    route = respx.post("https://a.example/rpc")
    route.side_effect = [
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None}),
        httpx.Response(200, json=RECEIPT),
    ]
    async with PolygonRpcClient(
        endpoints=["https://a.example/rpc"],
    ) as client:
        result = await client.get_transaction_receipt("0x1")
    assert result is not None
    assert route.call_count == 2


def test_rpc_alchemy_ditaruh_paling_depan() -> None:
    client = PolygonRpcClient(
        endpoints=["https://pub.example"],
        alchemy_url="https://key.example/v2/abc",
    )
    assert client.endpoint_count == 2
    assert client._endpoints[0] == "https://key.example/v2/abc"


def test_rpc_tanpa_endpoint_ditolak() -> None:
    with pytest.raises(ValueError, match="minimal satu endpoint"):
        PolygonRpcClient(endpoints=[])
