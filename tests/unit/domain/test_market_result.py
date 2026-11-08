"""Unit test T-006: MarketResult, transisi status, pemetaan outcomePrices.

Tanpa I/O, tanpa import framework.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from polywatch.domain.errors import (
    InvalidMarketStatusTransitionError,
    InvalidOutcomePricesError,
    InvalidResolutionStatusError,
)
from polywatch.domain.market import (
    MarketResult,
    MarketSlug,
    MarketStatus,
    Outcome,
    can_transition,
    outcome_from_outcome_prices,
    status_at,
    transition_status,
    validate_uma_resolution_status,
)

START = datetime.fromtimestamp(1762560000, tz=UTC)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (MarketStatus.UPCOMING, MarketStatus.LIVE),
        (MarketStatus.UPCOMING, MarketStatus.CLOSED),
        (MarketStatus.LIVE, MarketStatus.CLOSED),
        (MarketStatus.CLOSED, MarketStatus.RESOLVED),
    ],
)
def test_transisi_legal(current: MarketStatus, target: MarketStatus) -> None:
    assert can_transition(current, target)
    assert transition_status(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # mundur
        (MarketStatus.LIVE, MarketStatus.UPCOMING),
        (MarketStatus.CLOSED, MarketStatus.LIVE),
        (MarketStatus.RESOLVED, MarketStatus.CLOSED),
        # lompat ilegal
        (MarketStatus.UPCOMING, MarketStatus.RESOLVED),
        (MarketStatus.LIVE, MarketStatus.RESOLVED),
        # terminal
        (MarketStatus.RESOLVED, MarketStatus.UPCOMING),
        # ke diri sendiri bukan transisi
        (MarketStatus.LIVE, MarketStatus.LIVE),
    ],
)
def test_transisi_ilegal(current: MarketStatus, target: MarketStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidMarketStatusTransitionError):
        transition_status(current, target)


def test_status_at_dari_waktu() -> None:
    slug = MarketSlug.parse("btc-updown-5m-1762560000")

    assert status_at(slug, START - timedelta(seconds=1)) is MarketStatus.UPCOMING
    assert status_at(slug, START) is MarketStatus.LIVE
    assert status_at(slug, slug.end_at - timedelta(seconds=1)) is MarketStatus.LIVE
    assert status_at(slug, slug.end_at) is MarketStatus.CLOSED


def test_upcoming_lewat_langsung_ke_closed() -> None:
    # market terlewat sama sekali: boleh melompat ke CLOSED, bukan RESOLVED
    slug = MarketSlug.parse("btc-updown-5m-1762560000")
    transition_status(MarketStatus.UPCOMING, status_at(slug, slug.end_at))
    with pytest.raises(InvalidMarketStatusTransitionError):
        transition_status(MarketStatus.UPCOMING, MarketStatus.RESOLVED)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["1", "0"]', Outcome.UP),
        ('["0", "1"]', Outcome.DOWN),
        ('["1.0", "0.00"]', Outcome.UP),
        ('["0.00", "1.000"]', Outcome.DOWN),
        ("[1, 0]", Outcome.UP),
    ],
)
def test_outcome_prices_valid(raw: str, expected: Outcome) -> None:
    assert outcome_from_outcome_prices(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [
        '["1", "0", "0"]',
        '["1"]',
        '["0.5", "0.5"]',
        "bukan-json",
        "",
        "null",
        '"1"',
    ],
)
def test_outcome_prices_invalid(raw: str) -> None:
    with pytest.raises(InvalidOutcomePricesError):
        outcome_from_outcome_prices(raw)


def test_outcome_prices_tipe_salah() -> None:
    with pytest.raises(InvalidOutcomePricesError):
        outcome_from_outcome_prices(12345)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("resolved", True),
        ("Resolved", True),
        ("pending", False),
        (None, False),
    ],
)
def test_uma_resolution_status_valid(raw: str | None, expected: bool) -> None:
    assert validate_uma_resolution_status(raw) is expected


def test_uma_resolution_status_tidak_dikenal() -> None:
    with pytest.raises(InvalidResolutionStatusError):
        validate_uma_resolution_status("weird-status")


def test_market_result_equality_dan_hash() -> None:
    a = MarketResult(
        market_id=1,
        open_price=Decimal("100.5"),
        close_price=Decimal("101"),
        result=Outcome.UP,
        volume_usdc=Decimal("42.068629"),
        price_source="polymarket-crypto-price",
        resolved_at=START,
    )
    b = MarketResult(
        market_id=1,
        open_price=Decimal("100.5"),
        close_price=Decimal("101"),
        result=Outcome.UP,
        volume_usdc=Decimal("42.068629"),
        price_source="polymarket-crypto-price",
        resolved_at=START,
    )
    c = MarketResult(
        market_id=1,
        open_price=Decimal("100.5"),
        close_price=Decimal("101"),
        result=Outcome.DOWN,  # beda hasil
        volume_usdc=Decimal("42.068629"),
        price_source="polymarket-crypto-price",
        resolved_at=START,
    )

    assert a == b
    assert hash(a) == hash(b)
    assert a != c
    assert a != "bukan MarketResult"
    assert len({a, b, c}) == 2
