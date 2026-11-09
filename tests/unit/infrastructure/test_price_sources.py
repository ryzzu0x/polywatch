"""Unit test price source adapter (respx mock) — Polymarket crypto-price & Binance."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from polywatch.infrastructure.prices.binance_price_source import (
    BinancePriceSource,
    parse_klines,
)
from polywatch.infrastructure.prices.polymarket_price_source import (
    PolymarketPriceSource,
    parse_crypto_price_payload,
)

POLY = "https://polymarket.com"
BINANCE = "https://api.binance.com"

# Window market untuk slug btc-updown-5m-1794110400:
# start 2026-11-07T04:00:00Z, end 04:05:00Z — sudah lampau → grace 10 menit
# terlewati, jadi butuh slug yang window-nya masih segar untuk kasus sukses.
FRESH_SLUG = "btc-updown-5m-9999999999"  # start jauh di masa depan


def test_parse_crypto_price_payload_valid() -> None:
    payload = {"openPrice": 75918.01, "closePrice": 75920.97, "timestamp": 1}
    pair = parse_crypto_price_payload(payload, "slug")
    assert pair == (Decimal("75918.01"), Decimal("75920.97"))


def test_parse_crypto_price_payload_none_bila_field_hilang() -> None:
    assert parse_crypto_price_payload({"openPrice": 1.0}, "slug") is None
    assert parse_crypto_price_payload(None, "slug") is None


def test_parse_crypto_price_payload_rusak_ditolak() -> None:
    with pytest.raises(ValueError, match="rusak"):
        parse_crypto_price_payload({"openPrice": "abc", "closePrice": "1"}, "slug")


def test_parse_klines_ambil_open_close() -> None:
    klines = [
        [1, "100.5", "101", "100", "100.9", 10],
        [2, "100.9", "102", "100.8", "101.7", 5],
    ]
    assert parse_klines(klines, "slug") == (Decimal("100.5"), Decimal("101.7"))


def test_parse_klines_kosong_none() -> None:
    assert parse_klines([], "slug") is None
    assert parse_klines(None, "slug") is None


@respx.mock
async def test_polymarket_open_close_sukses() -> None:
    respx.get(f"{POLY}/api/crypto/crypto-price").mock(
        return_value=httpx.Response(200, json={"openPrice": 1.5, "closePrice": 1.6})
    )
    source = PolymarketPriceSource(POLY)

    pair = await source.open_close(FRESH_SLUG)

    assert pair == (Decimal("1.5"), Decimal("1.6"))


@respx.mock
async def test_polymarket_window_lama_dilewati_tanpa_request() -> None:
    """Window historis: eventStartTime diabaikan server → tidak dipakai."""
    route = respx.get(f"{POLY}/api/crypto/crypto-price").mock(
        return_value=httpx.Response(200, json={"openPrice": 1, "closePrice": 2})
    )
    source = PolymarketPriceSource(POLY)

    assert await source.open_close("btc-updown-5m-1794110400") is None
    assert route.call_count == 0


@respx.mock
async def test_polymarket_gagal_setelah_retry_habis() -> None:
    """T-030/ADR-017: kegagalan permanen mengembalikan None (bukan raise)
    supaya use case bisa lanjut ke sumber cadangan / sentinel unavailable."""
    respx.get(f"{POLY}/api/crypto/crypto-price").mock(return_value=httpx.Response(503))
    source = PolymarketPriceSource(POLY)

    assert await source.open_close(FRESH_SLUG) is None


@respx.mock
async def test_binance_open_close_sukses() -> None:
    klines = [
        [1, "100.5", "101", "100", "100.9", 10],
        [2, "100.9", "102", "100.8", "101.7", 5],
    ]
    route = respx.get(f"{BINANCE}/api/v3/klines").mock(
        return_value=httpx.Response(200, json=klines)
    )
    source = BinancePriceSource(BINANCE)

    pair = await source.open_close("btc-updown-5m-1794110400")

    assert pair == (Decimal("100.5"), Decimal("101.7"))
    request = route.calls.last.request
    assert "symbol=BTCUSDT" in str(request.url)
    assert "interval=1m" in str(request.url)


@respx.mock
async def test_binance_kosong_mengembalikan_none() -> None:
    respx.get(f"{BINANCE}/api/v3/klines").mock(return_value=httpx.Response(200, json=[]))
    # T-030: mirror juga kosong — hasil tetap None.
    respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        return_value=httpx.Response(200, json=[])
    )
    source = BinancePriceSource(BINANCE)

    assert await source.open_close("btc-updown-5m-1794110400") is None


@respx.mock
async def test_binance_gagal_setelah_retry_habis() -> None:
    """T-030/ADR-017: gagal di host utama → coba mirror publik; gagal
    semuanya → None (bukan raise) supaya use case bisa fallback."""
    respx.get(f"{BINANCE}/api/v3/klines").mock(return_value=httpx.Response(429))
    mirror = respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        return_value=httpx.Response(200, json=[])
    )
    source = BinancePriceSource(BINANCE)

    assert await source.open_close("btc-updown-5m-1794110400") is None
    assert mirror.called  # mirror benar-benar dicoba sebagai upaya terakhir


@respx.mock
async def test_binance_host_utama_gagal_mirror_berhasil() -> None:
    """T-030/ADR-017: host utama 429 → mirror mengembalikan klines → dipakai."""
    klines = [
        [1, "100.5", "101", "100", "100.9", 10],
        [2, "100.9", "102", "100.8", "101.7", 5],
    ]
    respx.get(f"{BINANCE}/api/v3/klines").mock(return_value=httpx.Response(429))
    respx.get("https://data-api.binance.vision/api/v3/klines").mock(
        return_value=httpx.Response(200, json=klines)
    )
    source = BinancePriceSource(BINANCE)

    pair = await source.open_close("btc-updown-5m-1794110400")

    assert pair == (Decimal("100.5"), Decimal("101.7"))


@respx.mock
async def test_binance_aset_tidak_diperdagangkan_langsung_none() -> None:
    """xau/xag tidak ada pair di Binance — None tanpa request sia-sia."""
    source = BinancePriceSource(BINANCE)

    assert await source.open_close("xau-updown-15m-1794110400") is None


def test_slug_tidak_valid_ditolak_minta_harga() -> None:
    import asyncio

    assert asyncio.run(PolymarketPriceSource(POLY).open_close("bukan-slug")) is None
    assert asyncio.run(BinancePriceSource(BINANCE).open_close("bukan-slug")) is None
