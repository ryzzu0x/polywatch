"""Ukuran snapshot DB polywatch untuk bukti T-028 (dijalankan via uv run)."""

import asyncio
import os

import asyncpg


async def main() -> None:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        print("trades =", await conn.fetchval("select count(*) from trades"))
        print("market_results =", await conn.fetchval("select count(*) from market_results"))
        print(
            "live_future =",
            await conn.fetchval(
                "select count(*) from markets where status='LIVE' and end_at > now()"
            ),
        )
        print("max_traded_at =", await conn.fetchval("select max(traded_at) from trades"))
        rows = await conn.fetch(
            "select status, count(*) n from markets group by status order by status"
        )
        for r in rows:
            print("status", r["status"], "=", r["n"])
        recent = await conn.fetch(
            "select tx_hash, wallet, market_id, traded_at from trades "
            "order by traded_at desc limit 3"
        )
        for r in recent:
            print("recent_trade:", r["traded_at"], r["tx_hash"], r["wallet"])
    finally:
        await conn.close()


asyncio.run(main())
