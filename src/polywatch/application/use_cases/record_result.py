"""Use case record_result: arsip hasil market yang sudah CLOSED (T-009).

Untuk market CLOSED yang belum punya baris `market_results`:

1. Poll resolusi resmi Gamma (`get_result`) sampai `umaResolutionStatus`
   resolved — delay terukur ±343 s setelah `endDate`, jadi polling wajib
   toleran: retry tiap `poll_interval_sec` (default 30 s).
2. Arah hasil (`Outcome`) HANYA dari `outcomePrices` resolusi resmi —
   resolusi resmi adalah sumber kebenaran; harga open/close hanya informasi.
3. Harga open/close dari `PriceSource` utama; bila None (mis. window sudah
   bergeser), fallback `PriceSource` cadangan. `price_source` selalu terisi.
4. Simpan `MarketResult` (idempoten) + transisi status market
   CLOSED → RESOLVED lewat aturan domain.
5. Setelah `timeout_sec` (default 15 menit) belum resolved: kirim notifikasi
   ke owner lewat `Notifier` (maksimal sekali per market) + log error
   terstruktur.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from polywatch.application.dto.market import MarketResultSnapshot, PendingMarket
from polywatch.application.ports import (
    Clock,
    MarketRepository,
    MarketSource,
    Notifier,
    PriceSource,
)
from polywatch.domain.market import (
    Market,
    MarketResult,
    MarketSlug,
    MarketStatus,
    Outcome,
    transition_status,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 30.0
DEFAULT_TIMEOUT_SEC = 15 * 60.0
UNAVAILABLE_PRICE_SOURCE = "unavailable"
#: T-029/ADR-016: default jumlah market CLOSED yang diproses per siklus.
DEFAULT_BATCH_SIZE = 10


@dataclass(frozen=True, slots=True)
class RecordResultSummary:
    """Hasil satu market setelah diproses (ringkasan untuk log/monitoring)."""

    market_id: int
    slug: str
    status: str  # "resolved" | "timeout"


@dataclass(frozen=True, slots=True)
class RecordResultCycleSummary:
    """Ringkasan satu siklus record_result (T-029) untuk log/audit."""

    processed: int
    resolved: int
    timeout: int
    duration_ms: int


@dataclass(slots=True)
class RecordResult:
    """Catat hasil resmi market CLOSED yang belum terarsip."""

    source: MarketSource
    repository: MarketRepository
    primary_price: PriceSource
    fallback_price: PriceSource | None = None
    notifier: Notifier | None = None
    owner_chat_id: int | None = None
    clock: Clock | None = None
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    #: T-029/ADR-016: maksimum market CLOSED yang diproses per siklus —
    #: sisanya ditangani siklus berikutnya supaya loop tidak pernah
    #: lebih lama dari interval.
    batch_size: int = DEFAULT_BATCH_SIZE
    sleep: Callable[[float], Awaitable[None]] | None = None  # di-inject test
    _notified: set[int] = field(default_factory=set, repr=False)

    async def execute_once(self, market: Market, market_id: int) -> RecordResultSummary:
        """Proses satu market CLOSED sampai resolved atau timeout."""
        elapsed = 0.0

        if await self.repository.has_result(market_id):
            # sudah terarsip — idempoten, tidak diproses ulang
            return RecordResultSummary(market_id, _slug_str(market), "resolved")

        while True:
            snapshot = await self.source.get_result(_slug_str(market))
            if snapshot is not None and snapshot.resolved and snapshot.outcome is not None:
                await self._record(market, market_id, snapshot)
                return RecordResultSummary(market_id, _slug_str(market), "resolved")

            if elapsed >= self.timeout_sec:
                await self._notify_timeout(market, market_id)
                return RecordResultSummary(market_id, _slug_str(market), "timeout")

            await self._sleep(self.poll_interval_sec)
            elapsed += self.poll_interval_sec

    async def execute(self) -> Sequence[RecordResultSummary | RecordResultCycleSummary]:
        """Proses market CLOSED yang belum punya market_results — batch terbatas.

        T-029: maksimum `batch_size` market per siklus (default 10); sisanya
        ditangani siklus berikutnya. Setiap market hanya dipoll **sekali**
        per siklus — market yang resolusinya belum keluar ditunda ke siklus
        berikutnya (delay resolusi Gamma ±343 s wajar melebihi interval 60 s;
        menunggu di dalam siklus membuat loop > interval — BUG-011).
        Notifikasi timeout tetap dievaluasi dari umur market (`end_at` +
        `timeout_sec` vs now), bukan dari jumlah poll. Return list ringkasan
        per market + satu `RecordResultCycleSummary` di akhir (durasi siklus).
        """
        import time as _time

        started = _time.monotonic()
        pending = await self.repository.list_pending_resolution(limit=self._effective_batch())
        results: list[RecordResultSummary] = []
        deferred = 0
        for item in pending:
            if await self.repository.has_result(item.market_id):
                continue  # sudah terarsip: idempoten lintas siklus
            market = _pending_to_market(item)
            slug_str = _slug_str(market)
            snapshot = await self.source.get_result(slug_str)
            if snapshot is not None and snapshot.resolved and snapshot.outcome is not None:
                await self._record(market, item.market_id, snapshot)
                results.append(RecordResultSummary(item.market_id, slug_str, "resolved"))
                continue
            # Belum resolved: tunda ke siklus berikutnya — satu poll per siklus.
            deferred += 1
            if self._is_timeout(market):
                await self._notify_timeout(market, item.market_id)
                results.append(RecordResultSummary(item.market_id, slug_str, "timeout"))
        cycle = RecordResultCycleSummary(
            processed=len(results),
            resolved=sum(1 for r in results if r.status == "resolved"),
            timeout=sum(1 for r in results if r.status == "timeout"),
            duration_ms=round((_time.monotonic() - started) * 1000),
        )
        logger.info(
            "record_result selesai",
            extra={
                "event": "record_result_cycle",
                "processed": cycle.processed,
                "resolved": cycle.resolved,
                "timeout": cycle.timeout,
                "deferred": deferred,
                "batch_size": self._effective_batch(),
                "duration_ms": cycle.duration_ms,
            },
        )
        return [*results, cycle]

    def _is_timeout(self, market: Market) -> bool:
        """Market melewati batas waktu resolusi (dihitung dari `end_at`)."""
        if self.clock is None:
            return False
        end_at = market.slug.end_at
        return (self.clock.now() - end_at).total_seconds() > self.timeout_sec

    def _effective_batch(self) -> int:
        return max(self.batch_size, 1)

    async def _record(self, market: Market, market_id: int, snapshot: MarketResultSnapshot) -> None:
        """Simpan market_results + transisi status ke RESOLVED (idempoten)."""
        slug_str = _slug_str(market)
        price_source, (open_price, close_price) = await self._open_close(slug_str)

        resolved_at = self.clock.now() if self.clock is not None else datetime.now(tz=UTC)
        result = MarketResult(
            market_id=market_id,
            open_price=open_price,
            close_price=close_price,
            result=snapshot.outcome or Outcome.UP,  # outcome wajib terisi (dijaga di atas)
            volume_usdc=snapshot.volume_usdc,
            price_source=price_source,
            resolved_at=resolved_at,
        )
        await self.repository.save_result(result)

        new_status = transition_status(market.status, MarketStatus.RESOLVED)
        await self.repository.update_status(market_id, new_status)

        logger.info(
            "market_results tersimpan",
            extra={
                "event": "market_result_saved",
                "market_id": market_id,
                "slug": slug_str,
                "outcome": result.result.value,
                "volume_usdc": str(result.volume_usdc),
                "price_source": price_source,
            },
        )

    async def _open_close(self, slug: str) -> tuple[str, tuple[Decimal, Decimal]]:
        """Harga open/close dari sumber utama, fallback bila None.

        `price_source` selalu terisi; bila kedua sumber gagal, dipakai
        sentinel `unavailable` dengan harga 0/0 supaya baris tetap tersimpan
        (arah hasil tetap dari resolusi resmi).
        """
        pair = await self.primary_price.open_close(slug)
        if pair is not None:
            return self.primary_price.name, pair
        if self.fallback_price is not None:
            pair = await self.fallback_price.open_close(slug)
            if pair is not None:
                return self.fallback_price.name, pair
        logger.warning(
            "open/close tidak tersedia dari kedua sumber harga",
            extra={"event": "price_unavailable", "slug": slug},
        )
        return UNAVAILABLE_PRICE_SOURCE, (Decimal("0"), Decimal("0"))

    async def _notify_timeout(self, market: Market, market_id: int) -> None:
        """Notifikasi owner sekali per market bila resolusi tidak keluar."""
        if market_id in self._notified:
            return
        self._notified.add(market_id)
        slug_str = _slug_str(market)
        logger.error(
            "resolusi market tidak keluar setelah batas waktu",
            extra={
                "event": "resolution_timeout",
                "market_id": market_id,
                "slug": slug_str,
                "timeout_sec": self.timeout_sec,
            },
        )
        if self.notifier is not None:
            await self.notifier.send(
                f"[polywatch] Resolusi market {slug_str} belum keluar "
                f"setelah {int(self.timeout_sec // 60)} menit; market ini di-skip "
                "dan perlu diperiksa manual.",
                chat_id=self.owner_chat_id,
            )

    async def _sleep(self, seconds: float) -> None:
        if self.sleep is not None:
            await self.sleep(seconds)
        else:
            await asyncio.sleep(seconds)


def _slug_str(market: Market) -> str:
    return (
        f"{market.slug.asset.symbol}-updown-{market.slug.timeframe.value}"
        f"-{int(market.slug.start_at.timestamp())}"
    )


def _pending_to_market(item: PendingMarket) -> Market:
    return Market(
        condition_id=item.condition_id,
        slug=MarketSlug.parse(item.slug),
        token_up="",
        token_down="",
        status=item.status,
    )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DEFAULT_TIMEOUT_SEC",
    "UNAVAILABLE_PRICE_SOURCE",
    "RecordResult",
    "RecordResultCycleSummary",
    "RecordResultSummary",
]
