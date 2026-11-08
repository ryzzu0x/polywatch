"""Integration test Gamma client dengan respx — fixture respons nyata T-005."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from polywatch.domain.market import Timeframe
from polywatch.infrastructure.polymarket.gamma_client import (
    GammaClientError,
    GammaMarketSource,
    parse_market_payload,
)

BASE = "https://gamma-api.polymarket.com"
FIXTURE = Path("docs/spikes/t005-gamma-market-sample.json")


@pytest.fixture
def sample_payload() -> dict[str, object]:
    """Objek market resolved nyata dari spike T-005 (respons asli Gamma)."""
    data = json.loads(FIXTURE.read_text())
    return dict(data["response_truncated"])


@pytest.fixture
def live_payload(sample_payload: dict[str, object]) -> dict[str, object]:
    """Market live: variasi fixture resolved (volume null saat live, belum closed)."""
    payload = dict(sample_payload)
    payload["closed"] = False
    payload["volume"] = None
    payload["umaResolutionStatus"] = None
    return payload


def test_parse_payload_resolved_nyata(sample_payload: dict[str, object]) -> None:
    snapshot = parse_market_payload(sample_payload)

    assert snapshot.slug == "btc-updown-5m-1794110400"
    assert snapshot.condition_id == (
        "0x5b6de30f06b74864fb66d1464f1a71ec883dab14415d1e69896889c9b3740ed0"
    )
    assert snapshot.token_up == (
        "22798572314924303751012276323563073863041915279869103020788023451812478400438"
    )
    assert snapshot.token_down == (
        "105446704431325901772229507736853505709989966369727960462358089532128228670452"
    )
    assert snapshot.volume_usdc == Decimal("42.068629")
    assert snapshot.closed is True


def test_parse_payload_live_volume_null(live_payload: dict[str, object]) -> None:
    snapshot = parse_market_payload(live_payload)

    assert snapshot.volume_usdc == Decimal("0")
    assert snapshot.closed is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(clobTokenIds='["satu"]'), id="token-tunggal"),
        pytest.param(lambda p: p.update(clobTokenIds="rusak-json"), id="token-rusak"),
        pytest.param(lambda p: p.update(clobTokenIds=None), id="token-hilang"),
        pytest.param(lambda p: p.update(outcomes='["Yes", "No"]'), id="outcomes-bukan-updown"),
        pytest.param(lambda p: p.update(slug=""), id="slug-kosong"),
        pytest.param(lambda p: p.pop("conditionId"), id="condition-hilang"),
        pytest.param(lambda p: p.update(closed="yes"), id="closed-bukan-bool"),
        pytest.param(lambda p: p.update(volume="abc"), id="volume-bukan-angka"),
    ],
)
def test_parse_payload_rusak_ditolak(
    sample_payload: dict[str, object], mutate: Callable[[dict[str, object]], None]
) -> None:
    payload = dict(sample_payload)
    mutate(payload)
    with pytest.raises(ValueError, match=r"hilang|rusak|tidak|bukan|Expecting value"):
        parse_market_payload(payload)


@respx.mock
async def test_list_markets_paginasi_dan_param(live_payload: dict[str, object]) -> None:
    second = dict(live_payload)
    second["slug"] = "eth-updown-5m-1794110500"
    second["conditionId"] = "0xcond2"
    page1 = [live_payload] * 100  # penuh → lanjut ke halaman 2
    page2 = [second]

    def route(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["closed"] == "false"
        assert params["order"] == "endDate"
        assert params["ascending"] == "true"
        assert params["limit"] == "100"
        # end_date_min sekitar now-1h
        min_dt = datetime.fromisoformat(params["end_date_min"])
        assert abs(min_dt - (datetime.now(tz=UTC) - timedelta(hours=1))) < timedelta(minutes=5)
        return httpx.Response(200, json=page1 if params["offset"] == "0" else page2)

    respx.get(f"{BASE}/markets").mock(side_effect=route)

    snapshots = await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)

    assert len(snapshots) == 101
    assert snapshots[0].slug == "btc-updown-5m-1794110400"
    assert snapshots[-1].slug == "eth-updown-5m-1794110500"


@respx.mock
async def test_list_markets_memfilter_timeframe(live_payload: dict[str, object]) -> None:
    """Market 15m tidak ikut saat diminta 5m (dan sebaliknya)."""
    m15 = dict(live_payload)
    m15["slug"] = "eth-updown-15m-1794110500"
    m15["conditionId"] = "0x15m"
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, json=[live_payload, m15]))

    five = await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)
    fifteen = await GammaMarketSource(BASE).list_markets(Timeframe.FIFTEEN_MINUTES)

    assert [s.slug for s in five] == ["btc-updown-5m-1794110400"]
    assert [s.slug for s in fifteen] == ["eth-updown-15m-1794110500"]


@respx.mock
async def test_list_markets_payload_rusak_di_skip(
    live_payload: dict[str, object],
) -> None:
    broken = dict(live_payload)
    broken["clobTokenIds"] = "rusak"
    broken_but_typed = dict(live_payload)
    broken_but_typed.pop("conditionId")

    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json=[broken, broken_but_typed, live_payload])
    )

    snapshots = await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)

    assert len(snapshots) == 1
    assert snapshots[0].closed is False


@respx.mock
async def test_list_markets_retry_lalu_sukses(live_payload: dict[str, object]) -> None:
    route = respx.get(f"{BASE}/markets").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(200, json=[live_payload]),
        ]
    )

    snapshots = await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)

    assert route.call_count == 3
    assert len(snapshots) == 1


@respx.mock
async def test_list_markets_gagal_setelah_retry_habis() -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(503))

    with pytest.raises(GammaClientError):
        await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)


@respx.mock
async def test_list_markets_respons_bukan_list() -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, json={"err": 1}))

    with pytest.raises(GammaClientError):
        await GammaMarketSource(BASE).list_markets(Timeframe.FIVE_MINUTES)


def test_fixture_dibaca() -> None:
    # memastikan fixture path dipakai berulang tanpa state bocor antar test
    text = FIXTURE.read_text()
    data = json.loads(text)
    assert "response_truncated" in data
