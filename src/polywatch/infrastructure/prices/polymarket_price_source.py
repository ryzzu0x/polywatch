"""Adapter PriceSource utama: Polymarket crypto-price (ADR-005).

`GET /api/crypto/crypto-price?symbol=<aset>&eventStartTime=<unix>` →
`{openPrice, closePrice, timestamp}` — harga Chainlink TWAP yang dipakai
resolusi. Batasan terukur (T-005): `eventStartTime` historis diabaikan
server (selalu window aktif) — open harus dicatat saat window berjalan,
close saat window berakhir. Method ini hanya berguna ketika window masih
aktif/sesaat setelahnya; untuk window yang sudah bergeser kembalikan None
supaya use case jatuh ke sumber cadangan.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import MarketSlug

logger = logging.getLogger(__name__)

PRICE_SOURCE_NAME = "polymarket-chainlink"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # detik

# window crypto-price dianggap kedaluwarsa: hasil tak lagi cocok untuk market
# yang end-nya sudah lama lewat (eventStartTime diabaikan server).
_WINDOW_GRACE = timedelta(minutes=10)


class PolymarketPriceSourceError(RuntimeError):
    """Permintaan crypto-price gagal setelah semua retry."""


def parse_crypto_price_payload(payload: object, slug: str) -> tuple[Decimal, Decimal] | None:
    """Parse `{openPrice, closePrice}`; None bila belum lengkap/bukan dict.

    Raise `ValueError` bila bentuk ada tapi nilainya rusak (bukan angka).
    """
    if not isinstance(payload, dict):
        return None
    raw_open = payload.get("openPrice")
    raw_close = payload.get("closePrice")
    if raw_open is None or raw_close is None:
        return None
    try:
        return Decimal(str(raw_open)), Decimal(str(raw_close))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"crypto-price rusak (slug={slug!r}): {payload!r}") from exc


class PolymarketPriceSource:
    """Implementasi `PriceSource` dari endpoint crypto-price Polymarket."""

    name = PRICE_SOURCE_NAME

    def __init__(self, base_url: str = "https://polymarket.com") -> None:
        self._base_url = base_url.rstrip("/")

    async def open_close(self, slug: str) -> tuple[Decimal, Decimal] | None:
        try:
            parsed = MarketSlug.parse(slug)
        except InvalidMarketSlugError:
            logger.warning("price source: slug tidak valid", extra={"slug": slug})
            return None

        # Window harus masih relevan: eventStartTime diabaikan server, jadi
        # untuk market lama respons tidak merepresentasikan window market ini.
        now = datetime.now(tz=UTC)
        if now > parsed.end_at + _WINDOW_GRACE:
            logger.info(
                "price source: window market sudah bergeser, tidak dipakai",
                extra={"slug": slug},
            )
            return None

        params = {
            "symbol": parsed.asset.symbol,
            "eventStartTime": str(int(parsed.start_at.timestamp())),
        }
        url = f"{self._base_url}/api/crypto/crypto-price"
        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.get(url, params=params)
                response.raise_for_status()
                return parse_crypto_price_payload(response.json(), slug)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1)) * (0.5 + random.random())
                    logger.warning(
                        "crypto-price gagal, retry",
                        extra={
                            "attempt": attempt,
                            "delay_s": round(delay, 3),
                            "error": str(exc),
                            "slug": slug,
                        },
                    )
                    await asyncio.sleep(delay)
        raise PolymarketPriceSourceError(
            f"crypto-price {url} gagal setelah {_MAX_RETRIES} percobaan (slug={slug!r})"
        ) from last_error
