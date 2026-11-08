"""Unit test domain market."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polywatch.domain.errors import InvalidAssetError, InvalidMarketSlugError
from polywatch.domain.market import (
    Asset,
    MarketSlug,
    Outcome,
    Timeframe,
    outcome_from_prices,
)


def test_parse_slug_5m() -> None:
    slug = MarketSlug.parse("btc-updown-5m-1762560000")

    assert slug.asset.symbol == "btc"
    assert slug.timeframe is Timeframe.FIVE_MINUTES
    assert slug.timeframe.seconds == 300
    assert slug.start_at == datetime.fromtimestamp(1762560000, tz=UTC)
    assert slug.end_at - slug.start_at == Timeframe.FIVE_MINUTES.duration


def test_parse_slug_15m_dengan_aset_baru() -> None:
    slug = MarketSlug.parse("hype-updown-15m-1762560900")

    assert slug.asset.symbol == "hype"
    assert slug.timeframe is Timeframe.FIFTEEN_MINUTES
    assert slug.timeframe.seconds == 900


def test_parse_slug_tidak_peka_huruf_besar() -> None:
    assert MarketSlug.parse("ETH-UPDOWN-5M-1762560000").asset.symbol == "eth"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "btc-updown-7m-1762560000",
        "btc-updown-5m",
        "btc-updown-5m-17625600",
        "btc_updown_5m_1762560000",
        "updown-5m-1762560000",
    ],
)
def test_parse_slug_tidak_valid(raw: str) -> None:
    with pytest.raises(InvalidMarketSlugError):
        MarketSlug.parse(raw)


def test_asset_menolak_simbol_kosong() -> None:
    with pytest.raises(InvalidAssetError):
        Asset("  ")


def test_is_live_at() -> None:
    slug = MarketSlug.parse("btc-updown-5m-1762560000")

    assert slug.is_live_at(slug.start_at)
    assert slug.is_live_at(slug.end_at.replace(microsecond=0) - slug.timeframe.duration / 2)
    assert not slug.is_live_at(slug.end_at)


def test_outcome_from_prices() -> None:
    assert outcome_from_prices(Decimal("100"), Decimal("101")) is Outcome.UP
    assert outcome_from_prices(Decimal("100"), Decimal("100")) is Outcome.UP
    assert outcome_from_prices(Decimal("100"), Decimal("99.99")) is Outcome.DOWN
