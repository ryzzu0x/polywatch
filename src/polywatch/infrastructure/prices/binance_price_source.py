"""Adapter PriceSource cadangan: Binance 1m klines (ADR-005).

Open = harga open kline menit pertama window, close = harga close kline
menit terakhir. Akurasi terukur 85,4% terhadap resolusi resmi — dipakai
sebagai fallback/cross-check, asal harga disimpan di kolom `price_source`.

T-030/ADR-017: kegagalan permanen (setelah retry + mirror publik
`data-api.binance.vision`) mengembalikan None, bukan raise — use case
`record_result` memakai None sebagai sinyal "sumber ini habis" dan
melanjutkan ke sumber berikutnya / sentinel `unavailable`, sehingga
kegagalan satu sumber harga tidak menjatuhkan siklus (BUG-012).
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

# T-030/ADR-017: fallback TERAKHIR saat api.binance.com (dan Polymarket
# crypto-price) sama-sama gagal — mirror publik data-api.binance.vision.
# Overhead hanya satu request per market yang sudah gagal di dua sumber
# lain, jadi tidak menambah beban pada jalur sehat.
_BINANCE_MIRROR_URL = "https://data-api.binance.vision"

# pasangan simbol Binance untuk aset yang dipantau (fallback: USDT generik)
_SYMBOL_SUFFIX = "USDT"

# aset yang tidak diperdagangkan di Binance (metals via CFD/spot lain) —
# klines Binance tidak akan pernah tersedia; langsung kembalikan None
# tanpa 3x retry sia-sia.
_UNSUPPORTED_SYMBOLS = frozenset({"xau", "xag"})


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

        # Aset tanpa pair di Binance: hindari 3x retry yang pasti gagal.
        if parsed.asset.symbol.lower() in _UNSUPPORTED_SYMBOLS:
            logger.info(
                "price source: aset tidak tersedia di Binance",
                extra={"slug": slug, "symbol": parsed.asset.symbol},
            )
            return None

        # T-030/ADR-017: coba host utama lalu mirror publik sebagai upaya
        # terakhir sebelum menyerah (tetap None, bukan raise, supaya use
        # case bisa fallback ke sumber lain / sentinel 'unavailable').
        params = {
            "symbol": f"{parsed.asset.symbol.upper()}{_SYMBOL_SUFFIX}",
            "interval": "1m",
            "startTime": str(int(parsed.start_at.timestamp() * 1000)),
            "endTime": str(int(parsed.end_at.timestamp() * 1000)),
            "limit": "1000",
        }
        for base_url in dict.fromkeys((self._base_url, _BINANCE_MIRROR_URL)):
            pair = await self._fetch_klines(base_url, params, slug)
            if pair is not None:
                return pair
        return None

    async def _fetch_klines(
        self, base_url: str, params: dict[str, str], slug: str
    ) -> tuple[Decimal, Decimal] | None:
        """Satu endpoint klines dengan retry; None bila gagal/kosong."""
        url = f"{base_url.rstrip('/')}/api/v3/klines"
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
        logger.warning(
            "klines Binance gagal setelah semua percobaan",
            extra={"event": "binance_klines_failed", "url": url, "slug": slug},
        )
        assert last_error is not None
        return None
