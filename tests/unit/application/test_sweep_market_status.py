"""Unit test use case sweep_market_status (T-028) — fake repository.

Sweep dua arah, memakai status_at domain:
- LIVE basi (end_at lewat) → CLOSED;
- UPCOMING lewat waktu → CLOSED (tak pernah LIVE);
- UPCOMING yang jendelanya berjalan → LIVE (discover lambat, T-028 run nyata);
- market sesuai waktu / transisi ilegal → tidak disentuh;
- idempoten: kandidat dengan status sudah sesuai tidak diproses ulang.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from polywatch.application.dto.market import StaleMarket
from polywatch.application.use_cases.sweep_market_status import SweepMarketStatus
from polywatch.domain.market import MarketStatus

NOW = datetime(2026, 11, 8, 11, 58, 0, tzinfo=UTC)
#: Start jendela 5m yang sedang berjalan pada NOW (11:55).
RUNNING = int(datetime(2026, 11, 8, 11, 55, tzinfo=UTC).timestamp())


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeRepository:
    """Repository in-memory: status per market_id, update terekam."""

    def __init__(self, candidates: Sequence[StaleMarket]) -> None:
        self.candidates = list(candidates)
        self.statuses = {item.market_id: item.status for item in candidates}
        self.updates: list[tuple[int, MarketStatus]] = []

    async def list_stale_open(self, now: datetime) -> Sequence[StaleMarket]:
        return self.candidates

    async def update_status(self, market_id: int, status: MarketStatus) -> None:
        self.updates.append((market_id, status))
        self.statuses[market_id] = status


def _stale(market_id: int, status: MarketStatus, ts: int) -> StaleMarket:
    return StaleMarket(
        market_id=market_id,
        condition_id=f"0x{market_id}",
        slug=f"btc-updown-5m-{ts}",
        status=status,
        end_at=datetime.fromtimestamp(ts, tz=UTC) + timedelta(minutes=5),
    )


async def test_live_basi_ditutup_ke_closed() -> None:
    """LIVE dengan end_at lewat (bug T-028) → CLOSED."""
    repo = FakeRepository([_stale(7, MarketStatus.LIVE, RUNNING - 600)])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 1
    assert summary.started == 0
    assert repo.statuses[7] is MarketStatus.CLOSED


async def test_upcoming_lewat_waktu_juga_ditutup() -> None:
    """UPCOMING yang tidak pernah LIVE pun waktunya sudah selesai → CLOSED."""
    repo = FakeRepository(
        [
            _stale(1, MarketStatus.LIVE, RUNNING - 600),
            _stale(2, MarketStatus.UPCOMING, RUNNING - 3600),
        ]
    )
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 2
    assert all(repo.statuses[i] is MarketStatus.CLOSED for i in (1, 2))


async def test_upcoming_jendela_berjalan_dibuka_ke_live() -> None:
    """UPCOMING padahal start_at <= now < end_at → LIVE (discover lambat)."""
    repo = FakeRepository([_stale(3, MarketStatus.UPCOMING, RUNNING)])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 0
    assert summary.started == 1
    assert repo.statuses[3] is MarketStatus.LIVE


async def test_live_dalam_jendela_tidak_disentuh() -> None:
    """LIVE yang waktunya memang berjalan → tidak ada update."""
    repo = FakeRepository([_stale(4, MarketStatus.LIVE, RUNNING)])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 0
    assert summary.started == 0
    assert repo.updates == []


async def test_upcoming_masa_depan_tidak_dibuka() -> None:
    """UPCOMING yang belum mulai (start_at > now) → bukan kandidat LIVE."""
    repo = FakeRepository([_stale(5, MarketStatus.UPCOMING, RUNNING + 600)])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.started == 0
    assert repo.updates == []


async def test_idempoten_tanpa_kandidat() -> None:
    """Repository tanpa kandidat → 0 update."""
    repo = FakeRepository([])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 0
    assert repo.updates == []


async def test_sweep_kedua_kali_tidak_memproses_ulang() -> None:
    """Setelah status sesuai waktu, market tidak diproses ulang."""
    repo = FakeRepository([_stale(7, MarketStatus.LIVE, RUNNING - 600)])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]

    first = await use_case.execute()
    repo.candidates = []  # repository tidak lagi mengembalikannya
    second = await use_case.execute()

    assert first.closed == 1
    assert second.closed == 0
    assert repo.updates == [(7, MarketStatus.CLOSED)]


async def test_kombinasi_tutup_dan_buka_satu_siklus() -> None:
    """Satu siklus menutup basi dan membuka berjalan sekaligus."""
    repo = FakeRepository(
        [
            _stale(1, MarketStatus.LIVE, RUNNING - 600),  # basi → CLOSED
            _stale(2, MarketStatus.UPCOMING, RUNNING),  # berjalan → LIVE
            _stale(3, MarketStatus.LIVE, RUNNING),  # sesuai → tidak disentuh
        ]
    )
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 1
    assert summary.started == 1
    assert repo.statuses[1] is MarketStatus.CLOSED
    assert repo.statuses[2] is MarketStatus.LIVE
    assert repo.statuses[3] is MarketStatus.LIVE
    assert (3, MarketStatus.LIVE) not in repo.updates


async def test_slug_tidak_valid_di_skip_tidak_gagal() -> None:
    """Kandidat dengan slug rusak di-skip dengan log, batch tetap jalan."""
    bad = StaleMarket(
        market_id=9,
        condition_id="0x9",
        slug="wsl2-wol-nfo-tidak-valid",
        status=MarketStatus.LIVE,
        end_at=NOW - timedelta(minutes=5),
    )
    good = _stale(7, MarketStatus.LIVE, RUNNING - 600)
    repo = FakeRepository([bad, good])
    use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
    summary = await use_case.execute()

    assert summary.closed == 1  # hanya yang valid diproses
    assert 9 not in repo.statuses or repo.statuses[9] is MarketStatus.LIVE


async def test_log_jumlah_dituliskan(caplog: pytest.LogCaptureFixture) -> None:
    """Ringkasan sweep ter-log terstruktur untuk bukti run nyata."""
    import logging

    repo = FakeRepository(
        [
            _stale(1, MarketStatus.LIVE, RUNNING - 600),
            _stale(2, MarketStatus.UPCOMING, RUNNING),
        ]
    )
    with caplog.at_level(
        logging.INFO, logger="polywatch.application.use_cases.sweep_market_status"
    ):
        use_case = SweepMarketStatus(repository=repo, clock=FakeClock(NOW))  # type: ignore[arg-type]
        await use_case.execute()

    done = next(r for r in caplog.records if "sweep status market selesai" in r.message)
    assert done.__dict__["closed"] == 1
    assert done.__dict__["started"] == 1
