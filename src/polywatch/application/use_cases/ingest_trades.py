"""Use case ingest_trades: trade real-time hybrid WS + RPC + backfill (T-011).

Alur (angka latency dari spike T-005c, 39 trade terukur):
1. `LiveTokenSource` memberi token Up/Down market **LIVE** pada
   assetxtimeframe yang **enabled** di `watch_config` (filter owner).
2. Subscribe stream WS (`TradeEventStream`) → event `last_trade_price`
   membawa `tx_hash` (latency event ~81 ms, T-005b).
3. Per event: dedupe `tx_hash` (cache in-memory) → fetch receipt
   (`ReceiptFetcher`) — event WS datang **pra-konfirmasi** (receipt null
   sampai tx masuk blok, T-005c), fetcher melakukan poll internal.
4. Map token id → market lewat `TokenMarketInfo.token_up`/`token_down`
   → domain `Trade` (kunci idempoten tx_hash, log_index, wallet) →
   simpan idempoten `TradeRepository.insert_many`
   (ON CONFLICT DO NOTHING). Latency e2e terukur median 855 ms.
5. `backfill(market_id, since)`: tutup gap lewat Data API v2
   (`TradeSource`; median trade tersedia 164 s — jalur lambat).

Hook `on_saved(trades)` dipanggil tiap batch baru — alert engine (T-017)
akan dihubungkan di sana; sekarang hanya hook, bukan implementasi alert.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.dto.trade import FilledOrderSnapshot
from polywatch.application.ports import (
    Clock,
    MarketRepository,
    ReceiptFetcher,
    TradeEventStream,
    TradeRepository,
    TradeSource,
)
from polywatch.domain.market import Outcome
from polywatch.domain.trade import Action, Trade

logger = logging.getLogger(__name__)

#: Alamat CTF Exchange V2 — taker pada fill "facing exchange" (operator
#: mengisi order publik lewat exchange sebagai counterparty). Dipakai
#: konstanta port (bukan import infrastructure) supaya aturan
#: ketergantungan layer tetap terjaga.
EXCHANGE_ADDRESS = "0xe111180000d2663c0091e4f400237545b87b996b"

BUY = 0
SELL = 1

OnSavedCallback = Callable[[Sequence[Trade]], Awaitable[None]]

DEFAULT_MAX_CACHE_TX = 10_000


@dataclass(slots=True)
class IngestStats:
    """Statistik satu siklus ingest untuk log terstruktur."""

    events_seen: int = 0
    tx_deduped: int = 0
    receipts_failed: int = 0
    trades_saved: int = 0
    latency_ms: list[float] = field(default_factory=list)


class IngestTrades:
    """Orkestrasi ingest trade hybrid WS+RPC dengan dedupe idempoten."""

    def __init__(
        self,
        stream: TradeEventStream,
        receipts: ReceiptFetcher,
        trades: TradeRepository,
        markets: MarketRepository,
        token_markets: Sequence[TokenMarketInfo],
        clock: Clock,
        on_saved: OnSavedCallback | None = None,
        data_api: TradeSource | None = None,
        max_cache_tx: int = DEFAULT_MAX_CACHE_TX,
    ) -> None:
        self._stream = stream
        self._receipts = receipts
        self._trades = trades
        self._markets = markets
        self._token_markets = list(token_markets)
        self._clock = clock
        self._on_saved = on_saved
        self._data_api = data_api
        self._max_cache_tx = max_cache_tx
        self._seen_tx: set[str] = set()

    async def run_once(self) -> IngestStats:
        """Satu siklus: subscribe token live, proses event sampai stream berakhir.

        Stream berakhir saat server menutup koneksi (1000 "all subscribed
        assets resolved" — semua market siklus ini selesai, T-005b/T-005c).
        Runner memanggil ulang `run_once` (dengan token market live baru)
        untuk siklus berikutnya.
        """
        token_map = _token_map(self._token_markets)
        stats = IngestStats()
        if not token_map:
            logger.info("tidak ada token live; siklus dilewati")
            return stats
        logger.info(
            "mulai ingest ws",
            extra={"tokens": len(token_map), "markets": len(self._token_markets)},
        )
        async for event in self._stream.stream(list(token_map)):
            stats.events_seen += 1
            if event.tx_hash in self._seen_tx:
                stats.tx_deduped += 1
                continue
            self._seen_tx.add(event.tx_hash)
            if len(self._seen_tx) > self._max_cache_tx:
                keep = sorted(self._seen_tx)[len(self._seen_tx) // 2 :]
                self._seen_tx = set(keep)
            received_at = self._clock.now().timestamp()
            await self._process_tx(event.tx_hash, token_map, stats, received_at)
        _log_cycle(stats)
        return stats

    async def _process_tx(
        self,
        tx_hash: str,
        token_map: dict[str, TokenMarketInfo],
        stats: IngestStats,
        received_at: float,
    ) -> None:
        """Fetch receipt satu tx, decode fill, simpan idempoten."""
        fills = await self._receipts.filled_orders(tx_hash)
        if fills is None:
            stats.receipts_failed += 1
            logger.warning("receipt gagal/timeout", extra={"tx_hash": tx_hash})
            return
        now = self._clock.now()
        trades: list[Trade] = []
        for fill in fills:
            market = token_map.get(str(fill.token_id))
            if market is None:
                continue  # fill di luar market yang dipantau
            trades.extend(_fill_to_trades(fill, market, now))
        if not trades:
            return
        saved = await self._trades.insert_many(trades)
        stats.trades_saved += saved
        stats.latency_ms.append((now.timestamp() - received_at) * 1000)
        if saved > 0 and self._on_saved is not None:
            await self._on_saved(trades)

    async def backfill(self, market_id: int, since: datetime | None = None) -> int:
        """Tutup gap trade market lewat Data API v2 (jalur lambat, 164 s).

        Dipanggil terpisah (runner/ops) saat WS/RPC gagal atau untuk market
        yang resolve sebelum sempat di-ingest. Idempoten lewat constraint
        DB yang sama. Return jumlah baris baru.
        """
        if self._data_api is None:
            raise RuntimeError("TradeSource Data API tidak dikonfigurasi untuk backfill")
        market = await self._markets.get_token_market(market_id)
        if market is None:
            logger.warning("backfill: market tidak ditemukan", extra={"market_id": market_id})
            return 0
        snapshots = await self._data_api.fetch_trades(market.condition_id, since)
        trades = [
            Trade(
                tx_hash=s.tx_hash,
                log_index=s.log_index,
                market_id=market_id,
                wallet=s.wallet,
                outcome=s.outcome,
                action=s.action,
                price=s.price,
                size=s.size,
                usdc=s.usdc,
                traded_at=s.traded_at,
            )
            for s in snapshots
        ]
        saved = await self._trades.insert_many(trades)
        logger.info(
            "backfill selesai",
            extra={"market_id": market_id, "fetched": len(snapshots), "saved": saved},
        )
        return saved


def _token_map(markets: Sequence[TokenMarketInfo]) -> dict[str, TokenMarketInfo]:
    return {m.token_up: m for m in markets} | {m.token_down: m for m in markets}


def _fill_to_trades(
    fill: FilledOrderSnapshot, market: TokenMarketInfo, now: datetime
) -> list[Trade]:
    """Konversi satu fill menjadi Trade maker (+ Trade taker bila wallet nyata).

    Maker = pemilik order (wallet yang sign order — identitas yang dipakai
    Data API sebagai `proxy_wallet`-side). Taker bisa berupa alamat
    exchange (fill "facing exchange" oleh operator) — wallet operator
    tetap wallet nyata, dicatat sebagai trade terpisah; kunci idempoten
    (tx_hash, log_index, wallet) membedakannya.
    """
    if fill.token_id == int(market.token_up):
        outcome = Outcome.UP
    elif fill.token_id == int(market.token_down):
        outcome = Outcome.DOWN
    else:  # pragma: no cover - hanya dipanggil setelah token cocok
        raise ValueError(f"token {fill.token_id} tidak cocok market {market.market_id}")
    action = Action.BUY if fill.side == BUY else Action.SELL
    price = (fill.usdc / fill.size) if fill.size > 0 else Decimal(0)
    common = {
        "tx_hash": fill.tx_hash,
        "log_index": fill.log_index,
        "market_id": market.market_id,
        "outcome": outcome,
        "action": action,
        "price": price,
        "size": fill.size,
        "usdc": fill.usdc,
        "traded_at": now,
    }
    trades = [Trade(wallet=fill.maker, **common)]  # type: ignore[arg-type]
    if fill.taker != EXCHANGE_ADDRESS:
        trades.append(Trade(wallet=fill.taker, **common))  # type: ignore[arg-type]
    return trades


def _log_cycle(stats: IngestStats) -> None:
    if not stats.latency_ms:
        logger.info(
            "siklus ingest selesai",
            extra={
                "events_seen": stats.events_seen,
                "tx_deduped": stats.tx_deduped,
                "receipts_failed": stats.receipts_failed,
                "trades_saved": stats.trades_saved,
            },
        )
        return
    ordered = sorted(stats.latency_ms)
    logger.info(
        "siklus ingest selesai",
        extra={
            "events_seen": stats.events_seen,
            "tx_deduped": stats.tx_deduped,
            "receipts_failed": stats.receipts_failed,
            "trades_saved": stats.trades_saved,
            "latency_median_ms": round(ordered[len(ordered) // 2], 1),
            "latency_p90_ms": round(ordered[int(len(ordered) * 0.9)], 1),
            "latency_max_ms": round(ordered[-1], 1),
        },
    )
