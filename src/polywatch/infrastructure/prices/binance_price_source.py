"""Adapter PriceSource cadangan: Binance 1m klines (ADR-005).

Open = harga open kline menit pertama window, close = harga close kline
menit terakhir. Akurasi terukur 85,4% terhadap resolusi resmi — dipakai
sebagai fallback/cross-check, asal harga disimpan di kolom `price_source`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from decimal import Decimal, InvalidOperation

import httpx

from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import MarketSlug

logger = logging.getLogger(__name__)

PRICE_SOURCE_NAME = "binance-1m"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # detik

# pasangan simbol Binance untuk aset yang dipantau (fallback: USDT generik)
_SYMBOL_SUFFIX = "USDT"


class BinancePriceSourceError(RuntimeError):
    """Permintaan klines gagal setelah semua retry."""


def parse_klines(klines: object, slug: str) -> tuple[Decimal, Decimal] | None:
    """Ambil (open, close) dari daftar kline Binance 1m window market.

    Kline: `[openTime, open, high, low, close, ...]`. Open diambil dari kline
    pertama, close dari kline terakhir. None bila daftar kosong.
    """
    if not isinstance(klines, list) or not klines:
        return None
    try:
        first, last = klines[0], klines[-1]
        return Decimal(str(first[1])), Decimal(str(last[4]))
    except (IndexError, InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"klines Binance rusak (slug={slug!r})") from exc


class BinancePriceSource:
    """Implementasi `PriceSource` cadangan dari klines 1m Binance."""

    name = PRICE_SOURCE_NAME

    def __init__(self, base_url: str = "https://api.binance.com") -> None:
        self._base_url = base_url.rstrip("/")

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        try:
            parsed = MarketSlug.parse(slug)
        except InvalidMarketSlugError:
            logger.warning("price source: slug tidak valid", extra={"slug": slug})
            return None

        params = {
            "symbol": f"{parsed.asset.symbol.upper()}{_SYMBOL_SUFFIX}",
            "interval": "1m",
            "startTime": str(int(parsed.start_at.timestamp() * 1000)),
            "endTime": str(int(parsed.end_at.timestamp() * 1000)),
            "limit": "1000",
        }
        url = f"{self._base_url}/api/v3/klines"
        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.get(url, params=params)
                response.raise_for_status()
                return parse_klines(response.json(), slug)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1)) * (0.5 + random.random())
                    logger.warning(
                        "klines Binance gagal, retry",
                        extra={
                            "attempt": attempt,
                            "delay_s": round(delay, 3),
                            "error": str(exc),
                            "slug": slug,
                        },
                    )
                    await asyncio.sleep(delay)
        raise BinancePriceSourceError(
            f"klines Binance {url} gagal setelah {_MAX_RETRIES} percobaan (slug={slug!r})"
        ) from last_error
