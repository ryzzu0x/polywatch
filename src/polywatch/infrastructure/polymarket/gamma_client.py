"""Adapter Gamma API Polymarket (implementasi port MarketSource).

T-024 — discovery berbasis slug per jendela waktu: pencarian market
live/upcoming lewat `GET /markets?slug=<slug>` (dipanggil use case
discover_markets yang menghitung slug-nya). Endpoint list tetap tersedia
sebagai supplement (T-007) dan hasilnya difilter anti-stale oleh pemanggil.

Field `clobTokenIds`/`outcomes` berupa string JSON, bukan list — parsing
wajib tahan error: catat & skip market rusak, jangan crash seluruh batch.
Retry dengan exponential backoff + jitter untuk error transient.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from polywatch.application.dto.market import MarketResultSnapshot, MarketSnapshot
from polywatch.domain.errors import InvalidMarketSlugError
from polywatch.domain.market import (
    MarketSlug,
    Timeframe,
    outcome_from_outcome_prices,
    validate_uma_resolution_status,
)

logger = logging.getLogger(__name__)

_MARKETS_PATH = "/markets"
_PAGE_LIMIT = 100
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # detik
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Gamma menolak offset > 2000 ("use /markets/keyset for deeper pagination").
_MAX_OFFSET = 2000
#: Rate limit discovery: jeda minimum antar-request slug (≤5 req/s, T-024).
_SLUG_FETCH_INTERVAL = 0.25


class GammaClientError(RuntimeError):
    """Permintaan Gamma gagal setelah semua retry (transient pun gagal)."""


def _parse_json_list(raw: str | None) -> list[str] | None:
    """Parse string JSON list; None berarti kosong, raise ValueError bila rusak."""
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"bukan list of string: {raw!r}")
    return value


def parse_market_payload(payload: dict[str, Any]) -> MarketSnapshot:
    """Parse satu objek market dari respons Gamma ke `MarketSnapshot`.

    Raise `ValueError` bila field wajib rusak — pemanggil skip market itu.
    """
    slug = payload.get("slug")
    condition_id = payload.get("conditionId")
    if not isinstance(slug, str) or not slug:
        raise ValueError("field slug hilang/tipe salah")
    if not isinstance(condition_id, str) or not condition_id:
        raise ValueError(f"field conditionId hilang/tipe salah (slug={slug!r})")

    tokens = _parse_json_list(payload.get("clobTokenIds"))
    if tokens is None or len(tokens) != 2:
        raise ValueError(f"clobTokenIds tidak berpasangan (slug={slug!r})")
    token_up, token_down = tokens[0], tokens[1]

    outcomes = _parse_json_list(payload.get("outcomes"))
    if outcomes is None or [o.lower() for o in outcomes] != ["up", "down"]:
        raise ValueError(f"outcomes bukan [Up, Down] (slug={slug!r})")

    try:
        volume = Decimal(str(payload.get("volume") or "0"))
    except InvalidOperation as exc:
        raise ValueError(f"volume tidak valid (slug={slug!r})") from exc

    closed = payload.get("closed")
    if not isinstance(closed, bool):
        raise ValueError(f"closed bukan bool (slug={slug!r})")

    return MarketSnapshot(
        slug=slug,
        condition_id=condition_id,
        token_up=token_up,
        token_down=token_down,
        volume_usdc=volume,
        closed=closed,
    )


class GammaMarketSource:
    """Implementasi `MarketSource` untuk daftar market live (endpoint Gamma).

    Gamma tidak punya filter timeframe; market Up/Down 5m/15m datang bercampur.
    Pemfilteran per timeframe dilakukan di sini lewat `MarketSlug.parse`,
    sehingga `list_markets(Timeframe.X)` hanya mengembalikan market timeframe X.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._last_fetch_monotonic = 0.0

    def _throttle(self) -> None:
        """Jeda minimum antar-request slug — rate limit Gamma ≤5 req/s (T-024)."""
        now_mono = asyncio.get_running_loop().time()
        elapsed = now_mono - self._last_fetch_monotonic
        if elapsed < _SLUG_FETCH_INTERVAL:
            time.sleep(_SLUG_FETCH_INTERVAL - elapsed)
        self._last_fetch_monotonic = time.monotonic()

    async def list_markets(
        self, timeframe: Timeframe, *, window_seconds: int | None = None
    ) -> list[MarketSnapshot]:
        """Ambil semua market Up/Down live untuk satu timeframe (paginasi offset).

        Jalur cadangan discovery (T-024) — pemanggil wajib memfilter hasil
        anti-stale (endDate dekat sekarang). `window_seconds` memakai pencarian
        slug `{tf}-{unix}` bila diisi: halaman belakang list-endpoint berisi
        market basi (Des 2025, tak pernah ditutup) yang mengotori hasil.
        Market dengan payload rusak atau slug di luar timeframe di-skip
        dengan log, bukan menggagalkan batch.
        """
        now = datetime.now(tz=UTC)
        params: dict[str, str] = {
            "closed": "false",
            "active": "true",
            "order": "endDate",
            "ascending": "true",
            "end_date_min": (now - timedelta(hours=1)).isoformat(),
            "end_date_max": (now + timedelta(hours=3)).isoformat(),
            "limit": str(_PAGE_LIMIT),
        }
        if window_seconds is not None:
            params["end_date_min"] = (now - timedelta(seconds=window_seconds)).isoformat()
        snapshots: list[MarketSnapshot] = []
        offset = 0
        while True:
            page = await self._get_markets_page({**params, "offset": str(offset)})
            for snapshot in self._parse_page(page, offset):
                try:
                    slug = MarketSlug.parse(snapshot.slug)
                except InvalidMarketSlugError:
                    continue  # slug non-updown bukan target discovery
                if slug.timeframe is timeframe:
                    snapshots.append(snapshot)
            if len(page) < _PAGE_LIMIT or offset + _PAGE_LIMIT >= _MAX_OFFSET:
                break
            offset += _PAGE_LIMIT
        return snapshots

    async def _get_markets_page(self, params: dict[str, str]) -> list[dict[str, Any]]:
        url = f"{self._base_url}{_MARKETS_PATH}"
        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    raise ValueError("respons Gamma bukan list")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1)) * (0.5 + random.random())
                    logger.warning(
                        "Gamma gagal, retry",
                        extra={
                            "attempt": attempt,
                            "delay_s": round(delay, 3),
                            "error": str(exc),
                            "path": _MARKETS_PATH,
                        },
                    )
                    await asyncio.sleep(delay)
        raise GammaClientError(f"Gamma {url} gagal setelah {_MAX_RETRIES} percobaan") from (
            last_error
        )

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        """Cari satu market lewat slug: `GET /markets?slug=<slug>` (T-024).

        `closed=False` untuk jendela berjalan/ke depan; `closed=True` untuk
        jendela ke belakang (market live hilang dari pencarian slug setelah
        end — temuan T-005). Market dengan payload rusak di-skip dengan log.
        """
        params: dict[str, str] = {"slug": slug, "closed": "true" if closed else "false"}
        page = await self._get_markets_page(params)
        for index, payload in enumerate(page):
            if payload.get("slug") != slug:
                continue
            try:
                return parse_market_payload(payload)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning(
                    "market Gamma di-skip: payload rusak",
                    extra={"slug": slug, "index": index, "error": str(exc)},
                )
                return None
        return None

    async def get_result(self, slug: str) -> MarketResultSnapshot | None:
        """Ambil hasil resolusi satu market lewat slug (hanya valid untuk yang resolved).

        Sesuai T-005: market hilang dari `?slug=` beberapa menit setelah end,
        tapi dengan `&closed=true` tetap muncul lengkap dengan hasil resolusi.
        Market belum resolved mengembalikan None. `outcome` dipetakan dari
        `outcomePrices` (resolusi resmi = sumber kebenaran).
        """
        params = {"slug": slug, "closed": "true"}
        page = await self._get_markets_page(params)
        for payload in page:
            if payload.get("slug") != slug:
                continue
            return self._parse_result(payload)
        return None

    def _parse_result(self, payload: dict[str, Any]) -> MarketResultSnapshot | None:
        """Parse payload market resolved; None bila belum ada resolusi resmi."""
        if not validate_uma_resolution_status(payload.get("umaResolutionStatus")):
            return None
        try:
            outcome = outcome_from_outcome_prices(payload["outcomePrices"])
        except KeyError:
            logger.warning(
                "payload hasil market tanpa outcomePrices, di-skip",
                extra={"slug": payload.get("slug")},
            )
            return None
        try:
            return MarketResultSnapshot(
                slug=str(payload["slug"]),
                open_price=None,
                close_price=None,
                volume_usdc=Decimal(str(payload.get("volume") or "0")),
                resolved=True,
                outcome=outcome,
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.warning(
                "payload hasil market rusak, di-skip",
                extra={"slug": payload.get("slug"), "error": str(exc)},
            )
            return None

    def _parse_page(self, page: Sequence[dict[str, Any]], offset: int) -> list[MarketSnapshot]:
        parsed: list[MarketSnapshot] = []
        for index, payload in enumerate(page):
            try:
                parsed.append(parse_market_payload(payload))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning(
                    "market Gamma di-skip: payload rusak",
                    extra={"offset": offset, "index": index, "error": str(exc)},
                )
        return parsed
