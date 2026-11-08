# T-005b SPIKE PROBE — THROWAWAY, bukan kode produksi.
# Mengukur CLOB WebSocket market channel Polymarket (wss://ws-subscriptions-clob.polymarket.com/ws/market)
# untuk market Up/Down 5m: latency event, bentuk pesan, field identitas trade, ketahanan koneksi.
# Jalankan: uv run python scripts/spike/t005b_ws_probe.py --duration 330
# Tanpa secret. Rate limit diuji sopan.

"""Spike T-005b: probe CLOB WebSocket market channel. Output ringkasan JSON ke stdout."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import websockets

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = "polywatch-spike-t005b/0.1"
SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]{2,10})-updown-(?P<tf>5m|15m)-(?P<start>\d{10})$")


def _slug_info(slug: str) -> dict[str, Any] | None:
    m = SLUG_RE.match(slug)
    if not m:
        return None
    return {"slug": slug, "asset": m["asset"], "tf": m["tf"], "start": int(m["start"])}


async def find_live_5m_markets(limit: int) -> list[dict[str, Any]]:
    """Cari market updown 5m yang sedang live via Gamma (end_date_min = now-1h)."""
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as client:
        offset = 0
        while len(out) < limit and offset <= 2000:
            params = {
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "end_date_min": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "limit": 100,
                "offset": offset,
            }
            resp = await client.get(f"{GAMMA}/markets", params=params)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for m in rows:
                info = _slug_info(m.get("slug", ""))
                if info is None or info["tf"] != "5m":
                    continue
                tokens = m.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except (TypeError, ValueError):
                        tokens = []
                if len(tokens) != 2:
                    continue
                out.append({**info, "conditionId": m.get("conditionId"), "clobTokenIds": tokens})
                if len(out) >= limit:
                    break
            offset += len(rows)
    return out


async def fetch_data_api_trades(condition_id: str) -> list[dict[str, Any]]:
    """Ambil semua trade Data API v2 untuk satu market (cursor pagination)."""
    rows: list[dict[str, Any]] = []
    cursor = ""
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as client:
        for _ in range(20):
            params: dict[str, Any] = {"condition": condition_id, "limit": 500}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(f"{DATA}/trades", params=params)
            resp.raise_for_status()
            body = resp.json()
            page = body.get("data") or []
            rows.extend(page)
            cursor = body.get("next_cursor") or ""
            if not cursor or not page:
                break
            await asyncio.sleep(1)
    return rows


def _event_ts_ms(ev: dict[str, Any]) -> int | None:
    for key in ("timestamp", "ts", "time"):
        val = ev.get(key)
        if isinstance(val, str) and val.isdigit():
            val = int(val)
        if isinstance(val, int) and val > 1_000_000_000_000:
            return val
        if isinstance(val, float) and val > 1_000_000_000_000:
            return int(val)
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            return int(val * 1000)
    return None


async def probe(
    markets: list[dict[str, Any]],
    duration_s: int,
    idle_timeout_s: int,
    subscribe_variants: bool,
) -> dict[str, Any]:
    token_to_slug = {
        tok: m["slug"]
        for m in markets
        for tok in (m["clobTokenIds"] or [])
        if isinstance(tok, str)
    }
    all_tokens = list(token_to_slug)
    result: dict[str, Any] = {
        "markets": [m["slug"] for m in markets],
        "tokens_subscribed": len(all_tokens),
        "events": [],
        "event_types": Counter(),
        "latencies_ms": [],
        "connect_log": [],
        "close_code": None,
        "close_reason": None,
        "idle_timeout_hit": False,
    }

    sub_msg = {"assets_ids": all_tokens, "type": "market"}
    try:
        async with websockets.connect(WS, ping_interval=20, open_timeout=30) as ws:
            result["connect_log"].append("open")
            await ws.send(json.dumps(sub_msg))
            if subscribe_variants:
                # coba varian kedua bentuk pesan subscribe
                await ws.send(json.dumps({"type": "market", "assets_ids": all_tokens}))
            last_rx = time.monotonic()
            t0 = time.monotonic()
            deadline = t0 + duration_s
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5, deadline - time.monotonic()))
                except TimeoutError:
                    if time.monotonic() - last_rx > idle_timeout_s:
                        result["idle_timeout_hit"] = True
                        break
                    # server Polymarket minta text-frame "PING"
                    await ws.send("PING")
                    continue
                last_rx = time.monotonic()
                now_ms = int(time.time() * 1000)
                if raw == "PONG" or raw == "PING":
                    result["event_types"][raw] += 1
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    result["event_types"]["<non-json>"] += 1
                    continue
                messages = payload if isinstance(payload, list) else [payload]
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    ev_type = msg.get("event_type") or msg.get("type") or "<unknown>"
                    result["event_types"][ev_type] += 1
                    if ev_type in ("book", "tick_size_change"):
                        continue  # snapshot besar, tidak dipakai latency
                    lat = None
                    ts = _event_ts_ms(msg)
                    if ts is not None:
                        lat = now_ms - ts
                        result["latencies_ms"].append(lat)
                    asset = msg.get("asset_id") or ""
                    result["events"].append(
                        {
                            "type": ev_type,
                            "asset_id": asset,
                            "slug": token_to_slug.get(asset),
                            "fields": sorted(k for k in msg if k not in ("bids", "asks", "buys", "sells")),
                            "sample": {k: v for k, v in msg.items() if k not in ("bids", "asks", "buys", "sells")},
                            "latency_ms": lat,
                        }
                    )
            result["close_code"] = ws.close_code
            result["close_reason"] = ws.close_reason
    except websockets.exceptions.WebSocketException as exc:
        result["connect_log"].append(f"error: {exc!r}")
    result["events"] = result["events"][:400]  # jaga ukuran output
    return result


def _pct(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return ordered[idx]


def summarize(probe_res: dict[str, Any]) -> dict[str, Any]:
    lats = [x for x in probe_res["latencies_ms"] if x is not None and x >= 0]
    return {
        "event_types": dict(probe_res["event_types"]),
        "n_latency_events": len(lats),
        "latency_ms_median": statistics.median(lats) if lats else None,
        "latency_ms_p90": _pct(lats, 90),
        "latency_ms_max": max(lats) if lats else None,
        "close_code": probe_res["close_code"],
        "close_reason": probe_res["close_reason"],
        "idle_timeout_hit": probe_res["idle_timeout_hit"],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=330)
    parser.add_argument("--markets", type=int, default=2)
    parser.add_argument("--idle-timeout", type=int, default=60)
    parser.add_argument("--subscribe-variants", action="store_true")
    parser.add_argument("--compare-data-api", action="store_true")
    args = parser.parse_args()

    markets = await find_live_5m_markets(args.markets)
    if not markets:
        print(json.dumps({"error": "tidak ada market updown 5m live ditemukan"}, ensure_ascii=False))
        return
    print(f"# market live: {[m['slug'] for m in markets]}", flush=True)

    probe_res = await probe(markets, args.duration, args.idle_timeout, args.subscribe_variants)
    summary = summarize(probe_res)

    # contoh field tiap tipe event
    field_examples: dict[str, list[dict[str, Any]]] = {}
    for ev in probe_res["events"]:
        field_examples.setdefault(ev["type"], [])
        if len(field_examples[ev["type"]]) < 2:
            field_examples[ev["type"]].append(ev["sample"])

    output = {
        "summary": summary,
        "field_examples": field_examples,
        "connect_log": probe_res["connect_log"],
        "tokens_subscribed": probe_res["tokens_subscribed"],
        "events": probe_res["events"],
    }

    if args.compare_data_api:
        for m in markets[:1]:
            trades = await fetch_data_api_trades(m["conditionId"])
            ws_hashes = {
                json.dumps(ev["sample"], sort_keys=True) for ev in probe_res["events"]
            }
            ltp = [e for e in probe_res["events"] if e["type"] == "last_trade_price"]
            output["data_api_compare"] = {
                "slug": m["slug"],
                "data_api_trades": len(trades),
                "ws_last_trade_events": len(ltp),
                "note": "perbandingan kasar; identitas trade tidak ada di WS bila field terbatas",
            }

    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
