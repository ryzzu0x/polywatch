"""Unit test use case discover_markets berbasis slug per jendela (T-024)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from polywatch.application.dto.market import MarketSnapshot, PendingMarket, TokenMarketInfo
from polywatch.application.use_cases.discover_markets import (
    DiscoverMarkets,
    build_slug,
    window_start,
)
from polywatch.domain.market import Market, MarketSlug, MarketStatus, Timeframe


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeSlugSource:
    """SlugSource palsu: snapshot tersedia untuk slug tertentu."""

    def __init__(self, by_slug: dict[str, MarketSnapshot]) -> None:
        self.by_slug = by_slug
        self.calls: list[tuple[str, bool]] = []

    async def fetch_by_slug(self, slug: str, *, closed: bool) -> MarketSnapshot | None:
        self.calls.append((slug, closed))
        return self.by_slug.get(slug)


class FakeAssets:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def list_symbols(self) -> list[str]:
        return self._symbols


class FakeRepository:
    """MarketRepository in-memory; upsert idempoten per condition_id."""

    def __init__(self) -> None:
        self.by_condition: dict[str, Market] = {}
        self.upsert_calls = 0

    async def upsert(self, market: Market) -> int:
        self.upsert_calls += 1
        if market.condition_id not in self.by_condition:
            self.by_condition[market.condition_id] = market
            return len(self.by_condition)
        return 1

    async def get_by_slug(self, slug: str) -> Market | None:
        for market in self.by_condition.values():
            if market.slug == MarketSlug.parse(slug):
                return market
        return None

    async def list_by_status(self, status: MarketStatus) -> Sequence[Market]:
        return [m for m in self.by_condition.values() if m.status is status]

    async def get_result(self, market_id: int) -> None:
        return None

    async def save_result(self, result: object) -> None:
        return None

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        return None

    async def has_result(self, market_id: int) -> bool:
        return False

    async def list_pending_resolution(self, limit: int) -> Sequence[PendingMarket]:
        return []

    async def get_token_market(self, market_id: int) -> TokenMarketInfo | None:
        return None


async def _no_sleep(_: float) -> None:
    return None


def _snapshot(slug: str, condition_id: str, volume: str = "10") -> MarketSnapshot:
    return MarketSnapshot(
        slug=slug,
        condition_id=condition_id,
        token_up="111",
        token_down="222",
        volume_usdc=Decimal(volume),
        closed=False,
    )


NOW = datetime(2026, 11, 8, 12, 3, 27, tzinfo=UTC)
#: Start jendela 5m yang sedang berjalan pada NOW (12:00 UTC).
W5 = int(datetime(2026, 11, 8, 12, 0, tzinfo=UTC).timestamp())
#: Start jendela 15m yang sedang berjalan pada NOW (12:00 UTC juga).
W15 = W5


# ---------------------------------------------------------------- window_start


def test_window_start_5m() -> None:
    assert window_start(NOW, Timeframe.FIVE_MINUTES) == datetime.fromtimestamp(W5, tz=UTC)


def test_window_start_15m() -> None:
    assert window_start(NOW, Timeframe.FIFTEEN_MINUTES) == datetime.fromtimestamp(W15, tz=UTC)


def test_window_start_tepat_di_batas() -> None:
    """now == kelipatan durasi → jendela berjalan dimulai di now itu sendiri."""
    boundary = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
    assert window_start(boundary, Timeframe.FIVE_MINUTES) == boundary
    assert window_start(boundary, Timeframe.FIFTEEN_MINUTES) == boundary
    # 12:05 bukan kelipatan 15m → jendela 15m berjalan tetap dimulai 12:00.
    at_1205 = datetime(2026, 11, 8, 12, 5, 0, tzinfo=UTC)
    assert window_start(at_1205, Timeframe.FIVE_MINUTES) == at_1205
    assert window_start(at_1205, Timeframe.FIFTEEN_MINUTES) == boundary


def test_window_start_offset_ke_depan_dan_belakang() -> None:
    base = datetime.fromtimestamp(W5, tz=UTC)
    for offset in (-3, -1, 0, 1, 2):
        assert window_start(NOW, Timeframe.FIVE_MINUTES, offset) == base + timedelta(
            minutes=5 * offset
        )


def test_window_start_detik_sisa_dibuang() -> None:
    now = datetime(2026, 11, 8, 12, 3, 59, tzinfo=UTC)
    assert window_start(now, Timeframe.FIVE_MINUTES) == datetime.fromtimestamp(W5, tz=UTC)


# ------------------------------------------------------------------- build_slug


def test_build_slug_jendela_berjalan() -> None:
    start = window_start(NOW, Timeframe.FIVE_MINUTES)
    assert build_slug("btc", Timeframe.FIVE_MINUTES, start) == f"btc-updown-5m-{W5}"


def test_build_slug_jendela_ke_depan() -> None:
    start = datetime.fromtimestamp(W5 + 900, tz=UTC)
    assert build_slug("eth", Timeframe.FIFTEEN_MINUTES, start) == f"eth-updown-15m-{W15 + 900}"


def test_build_slug_jendela_ke_belakang() -> None:
    start = datetime.fromtimestamp(W5 - 900, tz=UTC)
    assert build_slug("sol", Timeframe.FIVE_MINUTES, start) == f"sol-updown-5m-{W5 - 900}"


def test_build_slug_validasi_balik_market_slug_parse() -> None:
    slug = build_slug("hype", Timeframe.FIVE_MINUTES, window_start(NOW, Timeframe.FIVE_MINUTES))
    parsed = MarketSlug.parse(slug)
    assert parsed.asset.symbol == "hype"
    assert parsed.timeframe is Timeframe.FIVE_MINUTES
    assert int(parsed.start_at.timestamp()) == W5


# --------------------------------------------------------------------- use case


async def test_discover_menyimpan_market_live_dari_jalur_slug() -> None:
    live = _snapshot(f"btc-updown-5m-{W5}", "0xa")
    source = FakeSlugSource({f"btc-updown-5m-{W5}": live})
    repo = FakeRepository()

    summary = await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    ).execute()

    assert summary.new_markets == 1
    assert summary.total_markets == 1
    assert summary.stale_filtered == 0
    assert repo.by_condition["0xa"].status is MarketStatus.LIVE


async def test_discover_query_jendela_depan_tanpa_closed_dan_belakang_closed() -> None:
    """offset < 0 memakai closed=true; berjalan/ke depan closed=false."""
    source = FakeSlugSource({})
    await DiscoverMarkets(
        source=source,
        repository=FakeRepository(),
        clock=FakeClock(NOW),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    ).execute()

    closed_by_offset = {}
    for slug, closed in source.calls:
        start = int(slug.rsplit("-", 1)[1])
        offset = (start - W5) // 300
        closed_by_offset[offset] = closed
    assert closed_by_offset[0] is False
    assert closed_by_offset[1] is False
    assert closed_by_offset[2] is False
    assert closed_by_offset[-1] is True
    assert closed_by_offset[-2] is True
    assert closed_by_offset[-3] is True


async def test_discover_upcoming_untuk_jendela_depan() -> None:
    upcoming = _snapshot(f"eth-updown-5m-{W5 + 600}", "0xb")
    source = FakeSlugSource({f"eth-updown-5m-{W5 + 600}": upcoming})
    repo = FakeRepository()

    await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("eth",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    ).execute()

    assert repo.by_condition["0xb"].status is MarketStatus.UPCOMING


async def test_discover_jendela_belakang_berstatus_closed() -> None:
    past = _snapshot(f"doge-updown-5m-{W5 - 300}", "0xc")
    source = FakeSlugSource({f"doge-updown-5m-{W5 - 300}": past})
    repo = FakeRepository()

    await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("doge",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    ).execute()

    assert repo.by_condition["0xc"].status is MarketStatus.CLOSED


async def test_discover_market_basi_ditolak() -> None:
    """Slug valid tapi endDate ~11 bulan lalu (Des 2025) → ditolak."""
    use_case = DiscoverMarkets(
        source=FakeSlugSource({}),
        repository=FakeRepository(),
        clock=FakeClock(NOW),
        default_assets=("eth",),
        sleep=_no_sleep,
    )
    parsed = use_case._stale_slug("eth-updown-5m-1766161800", NOW)
    assert parsed is None

    # jalur supplement: list-endpoint memang pernah mengembalikan market basi
    stale = _snapshot("eth-updown-5m-1766161800", "0xstale")

    class SupplementSource:
        async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
            return [stale]

    repo = FakeRepository()
    summary = await DiscoverMarkets(
        source=FakeSlugSource({}),
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("eth",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
        supplement_source=SupplementSource(),  # type: ignore[arg-type]
    ).execute()

    assert summary.total_markets == 0
    assert summary.stale_filtered >= 1
    assert repo.upsert_calls == 0
    assert "0xstale" not in repo.by_condition


async def test_discover_jendela_belakang_terlalu_jauh_ditolak_anti_stale() -> None:
    """Filter anti-stale: endDate 7 jam lalu (di luar ±6 jam) ditolak, 1 jam lalu lolos."""
    use_case = DiscoverMarkets(
        source=FakeSlugSource({}),
        repository=FakeRepository(),
        clock=FakeClock(NOW),
        default_assets=("xrp",),
        sleep=_no_sleep,
    )
    far_past = f"xrp-updown-5m-{W5 - 3600 * 7}"
    recent_past = f"xrp-updown-5m-{W5 - 600}"  # end 10 menit lalu — masih segar
    assert use_case._stale_slug(far_past, NOW) is None
    assert use_case._stale_slug(recent_past, NOW) is not None


async def test_discover_kedua_kali_tidak_menduplikasi() -> None:
    live = _snapshot(f"btc-updown-5m-{W5}", "0xa")
    source = FakeSlugSource({f"btc-updown-5m-{W5}": live})
    repo = FakeRepository()
    use_case = DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    )

    first = await use_case.execute()
    second = await use_case.execute()

    assert first.new_markets == 1
    assert second.new_markets == 0
    assert second.total_markets == 1
    assert len(repo.by_condition) == 1


async def test_discover_aset_digabung_dikenal_dan_default() -> None:
    """Aset = union tabel assets + default; tidak ada duplikat, urut alfabetis."""
    known = FakeAssets(["zec", "bnb"])
    source = FakeSlugSource({})
    use_case = DiscoverMarkets(
        source=source,
        repository=FakeRepository(),
        clock=FakeClock(NOW),
        known_assets=known,
        default_assets=("btc", "zec", "sol"),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    )
    assets = await use_case._assets()
    assert assets == ("bnb", "btc", "sol", "zec")


async def test_discover_slug_dari_aset_dikenal_diquiry() -> None:
    slug = f"zec-updown-5m-{W5}"
    source = FakeSlugSource({slug: _snapshot(slug, "0xz")})
    repo = FakeRepository()

    await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        known_assets=FakeAssets(["zec"]),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
    ).execute()

    assert any(slug == s for s, _ in source.calls)
    assert "0xz" in repo.by_condition
    assert all(m.slug.asset.symbol in {"btc", "zec"} for m in repo.by_condition.values())


async def test_discover_5m_dan_15m_diproses() -> None:
    s5 = _snapshot(f"btc-updown-5m-{W5}", "0x5")
    s15 = _snapshot(f"btc-updown-15m-{W15}", "0x15")
    source = FakeSlugSource({f"btc-updown-5m-{W5}": s5, f"btc-updown-15m-{W15}": s15})
    repo = FakeRepository()

    summary = await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("btc",),
        sleep=_no_sleep,
    ).execute()

    assert set(repo.by_condition) == {"0x5", "0x15"}
    assert summary.total_markets == 2


@pytest.mark.parametrize("asset", ["btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec"])
def test_build_slug_delapan_aset_live(asset: str) -> None:
    """8 aset live yang diukur saat T-024 semuanya menghasilkan slug valid."""
    slug = build_slug(asset, Timeframe.FIVE_MINUTES, window_start(NOW, Timeframe.FIVE_MINUTES))
    assert MarketSlug.parse(slug).asset.symbol == asset


async def test_supplement_menolak_market_basi_dari_list_endpoint() -> None:
    """List-endpoint mengembalikan market basi Des 2025 → tidak disimpan."""
    stale = _snapshot("eth-updown-5m-1766161800", "0xstale2")
    live = _snapshot(f"btc-updown-5m-{W5}", "0xlive")

    class SupplementSource:
        async def list_markets(self, timeframe: Timeframe) -> Sequence[MarketSnapshot]:
            return [stale, live]

    source = FakeSlugSource({})
    repo = FakeRepository()

    summary = await DiscoverMarkets(
        source=source,
        repository=repo,
        clock=FakeClock(NOW),
        default_assets=("btc",),
        timeframes=(Timeframe.FIVE_MINUTES,),
        sleep=_no_sleep,
        supplement_source=SupplementSource(),  # type: ignore[arg-type]
    ).execute()

    assert "0xstale2" not in repo.by_condition
    assert "0xlive" in repo.by_condition
    assert summary.stale_filtered >= 1
