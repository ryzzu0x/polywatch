# T-005c SPIKE PROBE — THROWAWAY, bukan kode produksi.
# Membuktikan: WS last_trade_price → transaction_hash → eth_getTransactionReceipt (RPC Polygon)
# → decode event OrderFilled → alamat wallet + tokenId + jumlah + arah. Mengukur latency
# end-to-end (event WS diterima → trade ter-decode & "tersimpan" dalam struktur Trade domain).
# Jalankan: uv run python scripts/spike/t005c_orderfilled_probe.py --duration 420
# RPC publik keyless; ALCHEMY_RPC_URL opsional lewat env (tidak pernah dicetak).

"""Spike T-005c: probe OrderFilled decode via RPC Polygon. Output ringkasan JSON + stdout."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import websockets

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PUBLIC_RPC = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
]
# Temuan empiris (T-005c): market updown live semuanya via CTF Exchange V2.
EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
# keccak OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
UA = "polywatch-spike-t005c/0.1"
# OrderFilled V2 non-indexed: side, tokenId, makerAmountFilled, takerAmountFilled,
# fee, builder, metadata
OF_TYPES = ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"]
# 6 desimal (USDC & outcome token Polymarket).
DECIMALS = 10**6
RECEIPT_WAIT_S = 8.0  # tx Polygon ~2s block time; tunggu receipt maksimal 8 s


def _decode_static(data_hex: str) -> tuple[int, int, int, int, int] | None:
    """Decode data log OrderFilled V2 tanpa library ABI (layout statis 7 word)."""
    raw = bytes.fromhex(data_hex[2:])
    if len(raw) != 7 * 32:
        return None
    words = [int.from_bytes(raw[i * 32 : (i + 1) * 32], "big") for i in range(5)]
    side, token_id, maker_amt, taker_amt, fee = words
    return side, token_id, maker_amt, taker_amt, fee


def _wallet(topic: str) -> str:
    return "0x" + topic[-40:]


async def find_live_updown(n: int) -> list[dict[str, Any]]:
    """Cari market updown live (end di masa depan) via Gamma; hasilkan token map."""
    out: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as client:
        for offset in range(0, 2100, 100):
            resp = await client.get(
                f"{GAMMA}/markets",
                params={
                    "closed": "false",
                    "order": "endDate",
                    "ascending": "true",
                    "end_date_min": (now - timedelta(hours=1)).isoformat(),
                    "limit": 100,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for m in rows:
                if "updown" not in (m.get("slug") or ""):
                    continue
                try:
                    end = datetime.fromisoformat((m["endDate"] or "").replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if end <= now:
                    continue
                tokens = m.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except (TypeError, ValueError):
                        tokens = []
                if not isinstance(tokens, list) or len(tokens) != 2:
                    continue
                out.append(
                    {
                        "slug": m["slug"],
                        "cid": m.get("conditionId"),
                        "tokens": [str(t) for t in tokens],
                    }
                )
                if len(out) >= n:
                    return out
    return out


async def measure_rpc_latency(samples: int = 20) -> dict[str, Any]:
    """Latensi eth_getTransactionReceipt per endpoint (tx recent Polymarket fixed)."""
    probe_tx = "0x27f890c0e9826bb939791021d77d8b4cd1a7dfee08bed3dce52cee3c26c30997"
    endpoints = list(PUBLIC_RPC)
    alchemy = os.environ.get("ALCHEMY_RPC_URL")
    if alchemy:
        endpoints.insert(0, alchemy)
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for ep in endpoints:
            label = "alchemy" if "alchemy" in ep else ep.replace("https://", "")
            lat: list[float] = []
            err = None
            for _ in range(samples):
                t0 = time.perf_counter()
                try:
                    r = await client.post(
                        ep,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "eth_getTransactionReceipt",
                            "params": [probe_tx],
                        },
                    )
                    r.json()["result"]["logs"]
                    lat.append((time.perf_counter() - t0) * 1000)
                except Exception as e:
                    err = repr(e)[:120]
                    break
            if lat:
                results[label] = {
                    "n": len(lat),
                    "median_ms": round(statistics.median(lat), 1),
                    "p90_ms": round(statistics.quantiles(lat, n=10)[8], 1)
                    if len(lat) >= 10
                    else None,
                    "max_ms": round(max(lat), 1),
                    "error": err,
                }
            else:
                results[label] = {"n": 0, "error": err}
    return results


def _pct(sorted_vals: Sequence[float], p: int) -> float:
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, max(0, (p * len(sorted_vals)) // 100))
    return sorted_vals[k]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=420)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--markets", type=int, default=4)
    args = parser.parse_args()

    rpc = os.environ.get("ALCHEMY_RPC_URL") or PUBLIC_RPC[0]
    rpc_label = "alchemy" if "ALCHEMY_RPC_URL" in os.environ else "publicnode"

    # tunggu market live berikutnya (siklus 5m/15m)
    mkts: list[dict[str, Any]] = []
    for _ in range(20):
        mkts = await find_live_updown(args.markets)
        if mkts:
            break
        print("menunggu market updown live berikutnya... (30 s)", flush=True)
        await asyncio.sleep(30)
    if not mkts:
        print("GIVE UP: tidak ada market updown live")
        return
    token_map = {t: m["slug"] for m in mkts for t in m["tokens"]}
    print("markets:", [m["slug"] for m in mkts], flush=True)

    trades: list[dict[str, Any]] = []
    rpc_client = httpx.AsyncClient(timeout=15)

    async def fetch_receipt(tx: str) -> tuple[dict[str, Any] | None, int]:
        """Poll receipt sampai tx masuk blok (WS datang pra-konfirmasi).

        Event WS tiba ~100-300 ms sebelum tx dieksekusi chain; receipt baru
        tersedia setelah block produced. Retry sampai `timeout_s`.
        """
        deadline = time.perf_counter() + RECEIPT_WAIT_S
        attempts = 0
        while time.perf_counter() < deadline:
            attempts += 1
            r = await rpc_client.post(
                rpc,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [tx],
                },
            )
            body = r.json()
            result = body.get("result")
            if result is not None:
                return result, attempts
            await asyncio.sleep(0.2)
        return None, attempts

    async with websockets.connect(WS, ping_interval=20) as ws:
        await ws.send(json.dumps({"assets_ids": list(token_map), "type": "market"}))
        deadline = time.time() + args.duration
        print(f"subscribe {len(token_map)} token, durasi {args.duration} s", flush=True)
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
            except TimeoutError:
                await ws.send("PING")
                continue
            except websockets.exceptions.ConnectionClosed as e:
                print("ws closed:", repr(e), flush=True)
                break
            if msg in ("PONG", "PING"):
                continue
            try:
                payload = json.loads(msg)
            except (TypeError, ValueError):
                continue
            items = payload if isinstance(payload, list) else [payload]
            for ev in items:
                if not isinstance(ev, dict) or ev.get("event_type") != "last_trade_price":
                    continue
                recv = time.time()
                tx = ev.get("transaction_hash")
                if not tx:
                    continue
                t0 = time.perf_counter()
                rc, attempts = await fetch_receipt(tx)
                rpc_ms = (time.perf_counter() - t0) * 1000
                decoded: list[dict[str, Any]] = []
                if rc:
                    for lg in rc.get("logs", []):
                        if lg.get("topics", [None])[0] != ORDER_FILLED_TOPIC:
                            continue
                        dec = _decode_static(lg.get("data", "0x"))
                        if dec is None:
                            continue
                        side, token_id, maker_amt, taker_amt, fee = dec
                        # wallet pihak yang bersangkutan: maker (order owner).
                        maker = _wallet(lg["topics"][2])
                        taker = _wallet(lg["topics"][3])
                        # BUY: makerAssetId=0(collateral), makerAmt=USDC, takerAmt=token
                        # SELL: makerAssetId=tokenId, makerAmt=token, takerAmt=USDC
                        if side == 0:  # BUY
                            size = Decimal(taker_amt) / DECIMALS
                            usdc = Decimal(maker_amt) / DECIMALS
                        else:  # SELL
                            size = Decimal(maker_amt) / DECIMALS
                            usdc = Decimal(taker_amt) / DECIMALS
                        price = (usdc / size) if size > 0 else Decimal(0)
                        decoded.append(
                            {
                                "log_index": int(lg["logIndex"], 16),
                                "maker": maker,
                                "taker": taker,
                                "side": "BUY" if side == 0 else "SELL",
                                "token_id": token_id,
                                "size": str(size),
                                "usdc": str(usdc),
                                "price": str(price),
                                "fee": fee,
                                "slug": token_map.get(str(token_id)),
                            }
                        )
                saved_at = time.time()
                trades.append(
                    {
                        "recv_epoch": recv,
                        "ws_event": {
                            "asset_id": ev.get("asset_id"),
                            "price": ev.get("price"),
                            "size": ev.get("size"),
                            "side": ev.get("side"),
                            "timestamp": ev.get("timestamp"),
                        },
                        "tx_hash": tx,
                        "rpc_ms": round(rpc_ms, 1),
                        "rpc_attempts": attempts,
                        "decoded": decoded,
                        "e2e_ms": round((saved_at - recv) * 1000, 1),
                    }
                )
                print(
                    f"trade#{len(trades)} tx={tx[:12]}.. rpc={rpc_ms:.0f}ms "
                    f"e2e={trades[-1]['e2e_ms']:.0f}ms decoded={len(decoded)}",
                    flush=True,
                )
                if len(trades) >= args.min_trades and time.time() > deadline - args.duration / 2:
                    pass  # tetap kumpulkan sampai deadline untuk sampel lebih besar
    await rpc_client.aclose()

    # agregasi
    e2e = sorted(t["e2e_ms"] for t in trades)
    rpc_lat = sorted(t["rpc_ms"] for t in trades)
    n_decoded = sum(1 for t in trades if t["decoded"])
    n_wallet = sum(1 for t in trades if any(d["slug"] for d in t["decoded"]))
    ws_latency = []
    for t in trades:
        ts = t["ws_event"]["timestamp"]
        if isinstance(ts, (int, float)) and ts > 1_000_000_000_000:
            ws_latency.append(t["recv_epoch"] * 1000 - ts)
    summary = {
        "started_at": datetime.now(UTC).isoformat(),
        "markets": [m["slug"] for m in mkts],
        "rpc_endpoint": rpc_label,
        "trades_total": len(trades),
        "trades_with_orderfilled": n_decoded,
        "trades_token_subscribed": n_wallet,
        "e2e_ms": {
            "median": round(statistics.median(e2e), 1) if e2e else None,
            "p90": round(_pct(e2e, 90), 1),
            "max": round(max(e2e), 1) if e2e else None,
        },
        "ws_latency_ms": {
            "median": round(statistics.median(ws_latency), 1) if ws_latency else None,
            "p90": round(_pct(sorted(ws_latency), 90), 1) if ws_latency else None,
            "max": round(max(ws_latency), 1) if ws_latency else None,
        },
        "rpc_receipt_ms": {
            "median": round(statistics.median(rpc_lat), 1) if rpc_lat else None,
            "p90": round(_pct(rpc_lat, 90), 1),
            "max": round(max(rpc_lat), 1) if rpc_lat else None,
        },
        "rpc_latency_per_endpoint": await measure_rpc_latency(15),
        "sample_decoded": trades[0]["decoded"][:2] if trades else [],
        "sample_tx": trades[0]["tx_hash"] if trades else None,
        "sample_ws_event": trades[0]["ws_event"] if trades else None,
    }
    print(json.dumps(summary, indent=1, default=str))
    with open(  # noqa: ASYNC230
        "docs/spikes/t005c-run.json", "w"
    ) as f:
        json.dump({"summary": summary, "trades": trades}, f, default=str)


if __name__ == "__main__":
    asyncio.run(main())
