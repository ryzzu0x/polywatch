# T-005 SPIKE PROBE — THROWAWAY, bukan kode produksi.
# Mengukur endpoint Gamma & Data API v2 Polymarket untuk market Up/Down 5m/15m.
# Temuan utama yang dibuktikan script ini:
#   1. Gamma /markets?closed=true&order=endDate&descending + end_date_max → market updown resolved.
#   2. Data API v1 SUDAH PENSIUN (410); gunakan /v2/trades?condition=<conditionId> (cursor-only).
#   3. Rate limit: 10 request berurutan tanpa jeda → semua 200, tidak ada header x-ratelimit-*.
# Jalankan: uv run python scripts/spike/t005_probe.py
# Tanpa secret. Rate limit diuji sopan (jeda antar request).

"""Spike T-005: probe API Polymarket (Gamma, Data v2). Output JSON ke stdout."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
UA = "polywatch-spike-t005/0.1"
SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]{2,10})-updown-(?P<tf>5m|15m)-(?P<start>\d{10})$")


def slug_info(slug: str) -> dict[str, Any] | None:
    m = SLUG_RE.match(slug)
    if not m:
        return None
    return {"slug": slug, "asset": m["asset"], "tf": m["tf"], "start": int(m["start"])}


def market_row(info: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    tokens = m.get("clobTokenIds") or []
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except (TypeError, ValueError):
            tokens = []
    return {
        **info,
        "conditionId": m.get("conditionId"),
        "clobTokenIds": tokens,
        "startDate": m.get("startDate"),
        "endDate": m.get("endDate"),
        "volume": m.get("volume"),
        "closed": m.get("closed"),
        "umaResolutionStatus": m.get("umaResolutionStatus"),
        "outcomePrices": m.get("outcomePrices"),
        "outcomes": m.get("outcomes"),
        "title": m.get("question"),
    }


async def fetch_updown_markets(
    client: httpx.AsyncClient, closed: bool, end_date_max: str, pages: int
) -> tuple[list[dict[str, Any]], list[float]]:
    """Ambil market Up/Down dari Gamma. closed=true → yang sudah resolve."""
    out: dict[str, dict[str, Any]] = {}
    lat: list[float] = []
    for offset in range(0, pages * 100, 100):
        r = await client.get(
            f"{GAMMA}/markets",
            params={
                "limit": 100,
                "offset": offset,
                "closed": str(closed).lower(),
                "order": "endDate",
                "ascending": "false" if closed else "true",
                "end_date_max": end_date_max,
            },
        )
        r.raise_for_status()
        lat.append(r.elapsed.total_seconds())
        for m in r.json():
            info = slug_info(m.get("slug", ""))
            if info is None:
                continue
            out[info["slug"]] = market_row(info, m)
        if not r.json():
            break
        await asyncio.sleep(1.0)
    return list(out.values()), lat


async def probe_trades(
    client: httpx.AsyncClient, condition_id: str, watch_s: int = 180, poll_s: float = 4
) -> dict[str, Any]:
    """Poll /v2/trades?condition=... ; ukur latency kemunculan trade baru & duplikat."""
    fields: set[str] = set()
    seen: set[tuple[Any, ...]] = set()
    dup_rows = 0
    total_rows = 0
    lat: list[float] = []
    sample_trade: dict[str, Any] | None = None
    t0 = time.time()
    while time.time() - t0 < watch_s:
        r = await client.get(f"{DATA}/trades", params={"condition": condition_id, "limit": 100})
        r.raise_for_status()
        rows = r.json().get("data", [])
        now = time.time()
        for t in rows:
            fields.update(t.keys())
            key = (
                t.get("transaction_hash"),
                t.get("proxy_wallet"),
                t.get("token_id"),
                str(t.get("price")),
                str(t.get("size")),
                t.get("timestamp"),
            )
            if key in seen:
                dup_rows += 1
            else:
                seen.add(key)
                ts = t.get("timestamp")
                if ts:
                    lat.append(now - ts)
            total_rows += 1
            if sample_trade is None:
                sample_trade = t
        await asyncio.sleep(poll_s)
    lat.sort()
    return {
        "condition_id": condition_id,
        "watch_s": watch_s,
        "trade_fields": sorted(fields),
        "n_unique_trades": len(seen),
        "n_rows_seen": total_rows,
        "n_duplicate_rows": dup_rows,
        "latency_s_median": statistics.median(lat) if lat else None,
        "latency_s_p90": lat[int(0.9 * len(lat))] if lat else None,
        "latency_s_min": lat[0] if lat else None,
        "latency_s_max": lat[-1] if lat else None,
        "n_new_trades": len(lat),
        "sample_trade": sample_trade,
    }


async def pagination_probe(
    client: httpx.AsyncClient, condition_id: str, max_rows: int = 5000
) -> dict[str, Any]:
    """Loop cursor pagination; cari jumlah trade & timestamp terlama/terbaru."""
    rows = 0
    cursor: str | None = None
    oldest: int | None = None
    newest: int | None = None
    pages = 0
    while rows < max_rows:
        params: dict[str, Any] = {"condition": condition_id, "limit": 500}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"{DATA}/trades", params=params)
        r.raise_for_status()
        body = r.json()
        data = body.get("data", [])
        pages += 1
        rows += len(data)
        for t in data:
            ts = t.get("timestamp")
            if ts:
                if oldest is None or ts < oldest:
                    oldest = ts
                if newest is None or ts > newest:
                    newest = ts
        cursor = body.get("pagination", {}).get("next_cursor")
        if not cursor or not data:
            break
        await asyncio.sleep(1.0)
    return {
        "condition_id": condition_id,
        "pages": pages,
        "total_rows": rows,
        "oldest_trade_ts": oldest,
        "newest_trade_ts": newest,
    }


async def rate_limit_probe(client: httpx.AsyncClient, base: str, path: str) -> dict[str, Any]:
    """Uji sopan: 10 request berurutan tanpa jeda; catat status & header."""
    statuses: list[int] = []
    headers: dict[str, str] = {}
    lat: list[float] = []
    for _ in range(10):
        r = await client.get(f"{base}{path}", params={"limit": 1})
        statuses.append(r.status_code)
        lat.append(r.elapsed.total_seconds())
        for k, v in r.headers.items():
            if "rate" in k.lower() or "limit" in k.lower():
                headers[k] = v
    return {
        "endpoint": f"{base}{path}",
        "statuses": statuses,
        "latency_s": [round(x, 3) for x in lat],
        "ratelimit_headers": headers or "tidak ada header x-ratelimit-*",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--watch-s", type=int, default=180)
    args = parser.parse_args()
    result: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat()}

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as client:
        now = datetime.now(UTC)
        end_max = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        live, lat_live = await fetch_updown_markets(client, False, end_max, args.pages)
        closed_m, lat_closed = await fetch_updown_markets(client, True, end_max, 2)
        per_hour: Counter[tuple[str, str, int]] = Counter()
        for m in live + closed_m:
            per_hour[(m["asset"], m["tf"], m["start"] // 3600)] += 1
        hours = sorted({h for (_a, _t, h) in per_hour if h > 1700000000})
        by_tf: Counter[str] = Counter()
        for (_asset, tf, _h), c in per_hour.items():
            by_tf[tf] += c
        result["gamma"] = {
            "endpoint": f"{GAMMA}/markets?closed=&order=endDate&end_date_max=",
            "n_live_markets": len(live),
            "n_closed_resolved_markets": len(closed_m),
            "latency_s_median": statistics.median(lat_live + lat_closed),
            "hours_covered": [str(datetime.fromtimestamp(h, UTC)) for h in hours],
            "markets_per_hour_avg": {
                tf: round(by_tf[tf] / max(len(hours), 1), 2) for tf in ("5m", "15m")
            },
            "assets_seen": sorted({m["asset"] for m in live + closed_m}),
            "sample_live_market": live[0] if live else None,
            "sample_closed_market": closed_m[0] if closed_m else None,
        }

        if live:
            cid = next((m["conditionId"] for m in live if m["conditionId"]), None)
            if cid:
                result["data_api_v2"] = {
                    "endpoint": f"{DATA}/trades?condition=<conditionId>",
                    "latency_probe": await probe_trades(client, cid, watch_s=args.watch_s),
                    "pagination_probe": await pagination_probe(client, cid),
                }

        result["rate_limit"] = [
            await rate_limit_probe(client, GAMMA, "/events"),
            await rate_limit_probe(client, DATA, "/trades"),
        ]

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
