"""Use case ingest_trades: trade real-time hybrid WS + RPC + backfill (T-011).

Alur (angka latency dari spike T-005c, 39 trade terukur):
1. `LiveTokenSource` memberi token Up/Down market **LIVE** pada
   assetxtimeframe yang **enabled** di `watch_config` (filter owner).
   Semantik T-026/ADR-012: baris `watch_config` hanya menyimpan *override*;
   pasangan tanpa baris dianggap enabled (default-enabled), sehingga tabel
   kosong = semua aset x timeframe aktif.
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

T-026 (langganan WS hidup kontinyu): `run()` adalah loop kontinyu —
satu langganan WS dipertahankan seumur proses. `LiveTokenSource`
memberi token market live baru; bila daftar berubah, stream
direstart dengan token baru (klik WS ditutup server tiap akhir
jendela 5m/15m dengan close 1000 "all subscribed assets resolved",
jadi restart koneksi adalah jalur normal, bukan siklus mati).

Hook `on_saved(trades)` dipanggil tiap batch baru — alert engine (T-017)
dihubungkan lewat container; evaluator dibuat per-panggilan.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from polywatch.application.dto.market import TokenMarketInfo
from polywatch.application.dto.trade import FilledOrderSnapshot
from polywatch.application.ports import (
    Clock,
    LiveTokenSource,
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

#: Callback heartbeat ingest (T-027/ADR-014): dipanggil setiap kali ingest
#: benar-benar bekerja (batch commit tersimpan / siklus WS aktif) — dasar
#: kesegaran ingest di HealthMonitor, bukan trade market LIVE.
OnHeartbeatCallback = Callable[[bool], Awaitable[None]]

#: T-030/ADR-017: event mentah dari stream (frame apa pun) — dipakai
#: watchdog untuk membedakan "stream hidup dan bicara" vs "hidup tapi
#: bisu". Implementasi adapter nyata mengembalikan acakan token yang
#: berubah tiap event; kelas ini hanya penanda protokol.
StreamRawEvent = object


class _Watchdog:
    """Pelacak event terakhir + timer paksa-reconnect (T-030/ADR-017).

    Stream "hidup tapi bisu" (BUG-013): koneksi WS terbuka, recv() tidak
    pernah timeout, tapi tidak ada event datang — token jendela lama. Lalu
    internal `asyncio.wait_for` memberi batas waktu berjalan stream tanpa
    event; bila habis, pemanggil me-restart koneksi dengan token terbaru.
    """

    def __init__(self, timeout_sec: float) -> None:
        self.timeout_sec = timeout_sec
        self.last_event_monotonic: float | None = None
        self.live_markets = 0
        self.forced = 0

    def mark_event(self) -> None:
        self.last_event_monotonic = asyncio.get_running_loop().time()

    def arm(self) -> None:
        """Mulai/jaw ulang penghitungan dari sekarang (stream baru dibuka)."""
        self.last_event_monotonic = asyncio.get_running_loop().time()

    def should_force(self, has_tokens: bool) -> bool:
        """True bila tidak ada event dalam `timeout_sec` padahal ada token."""
        if not has_tokens or self.timeout_sec <= 0:
            return False
        if self.last_event_monotonic is None:
            return False  # belum pernah dibuka — biarkan stream mulai
        elapsed = asyncio.get_running_loop().time() - self.last_event_monotonic
        return elapsed >= self.timeout_sec


DEFAULT_MAX_CACHE_TX = 10_000

#: Interval refresh token market live untuk stream kontinyu (T-026).
DEFAULT_LIVE_REFRESH_SEC = 30.0

#: T-030/ADR-017: watchdog — bila TIDAK ada event WS sama sekali selama
#: N detik padahal ada market live (stream hidup tapi bisu), paksa
#: reconnect + resubscribe. Koneksi "hidup tapi bisu" (BUG-013) tidak
#: memicu reconnect otomatis klien WS karena recv() tidak timeout.
DEFAULT_WATCHDOG_SEC = 180.0

#: T-030/ADR-017: interval log heartbeat ingest (audit dari journald).
DEFAULT_HEARTBEAT_LOG_SEC = 60.0


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
        clock: Clock,
        live_tokens: LiveTokenSource | None = None,
        token_markets: Sequence[TokenMarketInfo] | None = None,
        on_saved: OnSavedCallback | None = None,
        data_api: TradeSource | None = None,
        max_cache_tx: int = DEFAULT_MAX_CACHE_TX,
        live_refresh_sec: float = DEFAULT_LIVE_REFRESH_SEC,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_batch_commit: Callable[[], Awaitable[None]] | None = None,
        on_heartbeat: OnHeartbeatCallback | None = None,
        watchdog_sec: float = DEFAULT_WATCHDOG_SEC,
        heartbeat_log_sec: float = DEFAULT_HEARTBEAT_LOG_SEC,
    ) -> None:
        self._stream = stream
        self._receipts = receipts
        self._trades = trades
        self._markets = markets
        self._clock = clock
        self._live_tokens = live_tokens
        self._token_markets = list(token_markets or [])
        self._on_saved = on_saved
        self._data_api = data_api
        self._max_cache_tx = max_cache_tx
        self._live_refresh_sec = live_refresh_sec
        self._sleep = sleep or asyncio.sleep
        self._on_batch_commit = on_batch_commit
        self._on_heartbeat = on_heartbeat
        self._watchdog_sec = watchdog_sec
        self._heartbeat_log_sec = heartbeat_log_sec
        self._seen_tx: set[str] = set()
        self._last_event_beat = 0.0
        self._watchdog = _Watchdog(watchdog_sec)
        # statistik kumulatif untuk log heartbeat (T-030)
        self._total_events = 0
        self._total_saved = 0
        self._last_event_ts: float | None = None  # epoch clock
        self._batch_errors = 0  # BUG-015: penghitung batch gagal (log rate-limited)

    async def _heartbeat(self, ok: bool = True) -> None:
        """Laporkan aktivitas ingest ke health monitor (T-027).

        Gagal melapor tidak boleh mengganggu loop ingest.
        """
        if self._on_heartbeat is None:
            return
        try:
            await self._on_heartbeat(ok)
        except Exception:
            logger.exception(
                "laporan heartbeat ingest gagal", extra={"event": "ingest_heartbeat_error"}
            )

    async def _heartbeat_event(self) -> None:
        """Heartbeat ringan per event WS — throttle internal 60 dtk.

        Stream hidup berjam-jam; event WS (apa pun isinya) bukti siklus
        aktif, tetapi tidak perlu melapor ribuan kali per menit.
        """
        now = self._clock.now().timestamp()
        if now - self._last_event_beat < 60.0:
            return
        self._last_event_beat = now
        await self._heartbeat(ok=True)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Loop ingest kontinyu (T-026) + watchdog & heartbeat log (T-030).

        - Refresh token market live dari `live_tokens` tiap
          `live_refresh_sec` (dan segera saat stream berakhir — market
          selesai menutup koneksi WS dengan close 1000).
        - Bila set token BERUBAH, stream lama dibatalkan dan koneksi baru
          dibuka dengan token terbaru (re-subscribe paksa — BUG-013: refresh
          token sebelumnya tidak pernah mengganti token stream yang hidup).
        - Watchdog (T-030/ADR-017): tidak ada event WS dalam `watchdog_sec`
          padahal ada token live → batalkan stream + buka koneksi baru,
          log `ws_watchdog_reconnect`.
        - Log heartbeat ingest tiap `heartbeat_log_sec` berisi `tokens`,
          `live_markets`, `events_seen`, `trades_saved`, `last_event_age_s`
          — status ingest bisa diaudit dari journald.
        - `stop_event` (opsional) mengakhiri loop untuk shutdown rapi.
        """
        if stop_event is None:
            stop_event = asyncio.Event()

        async def _refresh_tokens(
            previous: list[TokenMarketInfo],
        ) -> list[TokenMarketInfo]:
            """Token live terbaru; saat gagal query → pertahankan sebelumnya."""
            if self._live_tokens is None:
                return self._token_markets or previous
            try:
                return list(await self._live_tokens.live_tokens())
            except Exception:
                logger.exception(
                    "gagal mengambil token live; pakai daftar sebelumnya",
                    extra={"event": "ingest_live_tokens_error"},
                )
                return previous

        current = await _refresh_tokens(list(self._token_markets))
        stats = IngestStats()
        last_hb_log = 0.0
        last_refresh_mono = asyncio.get_running_loop().time()
        while not stop_event.is_set():
            token_map = _token_map(current)
            self._watchdog.live_markets = len(current)
            if token_map:
                logger.info(
                    "mulai ingest ws",
                    extra={"tokens": len(token_map), "markets": len(current)},
                )
                stats = IngestStats()
                # Heartbeat: langganan WS aktif dimulai (T-027/ADR-014) —
                # stream kontinyu bisa hidup berjam-jam tanpa pernah
                # "berakhir", jadi siklus-hidup itu sendiri adalah aktivitas.
                await self._heartbeat(ok=True)
                self._watchdog.arm()
                stream_task = asyncio.ensure_future(self._drain_stream(token_map, stats))
                try:
                    while not stream_task.done() and not stop_event.is_set():
                        # tunggu dengan batas supaya watchdog/heartbeat log/
                        # refresh token tetap dievaluasi meski stream diam.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(asyncio.shield(stream_task), timeout=1.0)
                        if self._watchdog.should_force(bool(token_map)):
                            self._watchdog.forced += 1
                            logger.warning(
                                "watchdog ingest: tidak ada event WS; paksa reconnect",
                                extra={
                                    "event": "ws_watchdog_reconnect",
                                    "no_event_s": round(self._watchdog.timeout_sec),
                                    "tokens": len(token_map),
                                    "forced": self._watchdog.forced,
                                },
                            )
                            stream_task.cancel()
                            await self._heartbeat(ok=False)
                            break
                        now_mono = asyncio.get_running_loop().time()
                        if now_mono - last_hb_log >= max(self._heartbeat_log_sec, 1.0):
                            last_hb_log = now_mono
                            self._log_ingest_heartbeat(len(token_map))
                        # BUG-013: refresh token juga saat stream masih hidup
                        # — market 5m berganti tiap 5 menit, jendela close-1000
                        # tidak selalu datang. Token berubah → stream dibuka
                        # ulang dengan token terbaru (re-subscribe paksa).
                        if now_mono - last_refresh_mono >= max(self._live_refresh_sec, 0.0):
                            last_refresh_mono = now_mono
                            new_tokens = await _refresh_tokens(current)
                            if new_tokens != current:
                                self._log_resubscribe(current, new_tokens)
                                current = new_tokens
                                stream_task.cancel()
                                break
                    if not stream_task.done():
                        stream_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await stream_task
                except Exception:
                    logger.exception(
                        "stream ingest gagal; restart dengan token terbaru",
                        extra={"event": "ingest_stream_error"},
                    )
                    await self._heartbeat(ok=False)
                _log_cycle(stats)
                # Heartbeat: siklus WS selesai dengan sehat (T-027/ADR-014).
                await self._heartbeat(ok=True)
                if stats.trades_saved > 0:
                    logger.info(
                        "batch trade tersimpan",
                        extra={
                            "event": "ingest_trades_saved",
                            "trades_saved": stats.trades_saved,
                        },
                    )
            else:
                logger.info(
                    "tidak ada token live; tunggu siklus berikutnya",
                    extra={"event": "ingest_no_tokens"},
                )
            # stream berakhir / tanpa token: tunggu interval (atau stop)
            # lalu refresh token market live.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._live_refresh_sec)
            last_refresh_mono = asyncio.get_running_loop().time()
            new_tokens = await _refresh_tokens(current)
            if new_tokens != current:
                self._log_resubscribe(current, new_tokens)
            current = new_tokens

    def _log_resubscribe(
        self, old: Sequence[TokenMarketInfo], new: Sequence[TokenMarketInfo]
    ) -> None:
        """Log re-subscribe dengan token lama vs baru (BUG-015).

        Soak test T-030 menunjukkan token stream tidak pernah berubah
        walau jendela market berganti — log ini membuat pertukaran set
        token bisa diaudit: jumlah token lama vs baru dan berapa yang
        benar-benar bertukar (bukan sekadar selisih jumlah, yang 0 bila
        jendela 5m berganti dengan jumlah market tetap sama).
        """
        old_tokens = set(_token_map(old))
        new_tokens = set(_token_map(new))
        logger.info(
            "token live berubah; re-subscribe",
            extra={
                "event": "ingest_resubscribe",
                "markets_old": len(old),
                "markets_new": len(new),
                "tokens_old": len(old_tokens),
                "tokens_new": len(new_tokens),
                "tokens_added": len(new_tokens - old_tokens),
                "tokens_removed": len(old_tokens - new_tokens),
            },
        )

    async def _drain_stream(
        self, token_map: dict[str, TokenMarketInfo], stats: IngestStats
    ) -> None:
        """Konsumsi stream sampai berakhir (atau dibatalkan watchdog/runner)."""
        async for event in self._stream.stream(list(token_map)):
            stats.events_seen += 1
            self._total_events += 1
            self._watchdog.mark_event()
            self._last_event_ts = self._clock.now().timestamp()
            # Heartbeat per event WS diterima (rate-limited internal
            # supaya tidak membanjiri monitor).
            await self._heartbeat_event()
            if event.tx_hash in self._seen_tx:
                stats.tx_deduped += 1
                continue
            self._seen_tx.add(event.tx_hash)
            if len(self._seen_tx) > self._max_cache_tx:
                keep = sorted(self._seen_tx)[len(self._seen_tx) // 2 :]
                self._seen_tx = set(keep)
            received_at = self._clock.now().timestamp()
            await self._process_tx(event.tx_hash, token_map, stats, received_at)

    def _log_ingest_heartbeat(self, tokens: int) -> None:
        """Log heartbeat ingest terstruktur tiap 60 dtk (T-030/ADR-017)."""
        last_age: float | str
        if self._last_event_ts is None:
            last_age = "n/a"
        else:
            last_age = round(self._clock.now().timestamp() - self._last_event_ts, 1)
        logger.info(
            "heartbeat ingest",
            extra={
                "event": "ingest_heartbeat",
                "tokens": tokens,
                "live_markets": self._watchdog.live_markets,
                "events_seen": self._total_events,
                "trades_saved": self._total_saved,
                "batch_errors": self._batch_errors,
                "last_event_age_s": last_age,
                "watchdog_forced": self._watchdog.forced,
            },
        )

    async def run_once(self) -> IngestStats:
        """Satu siklus: proses event dari token saat ini sampai stream berakhir.

        Dipertahankan untuk kompatibilitas test/e2e (T-011): stream fake
        berakhir setelah semua event — stats siklus dikembalikan. Runner
        produksi memakai `run()` (kontinyu) sejak T-026.
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
        saved = await self._insert_batch(trades, stats)
        stats.latency_ms.append((now.timestamp() - received_at) * 1000)
        if saved > 0:
            if self._on_batch_commit is not None:
                # commit per batch (T-026): stream kontinyu tidak pernah
                # "berakhir", jadi commit tidak boleh menunggu akhir siklus.
                await self._on_batch_commit()
            if self._on_saved is not None:
                await self._on_saved(trades)

    async def _insert_batch(self, trades: list[Trade], stats: IngestStats) -> int:
        """Simpan satu batch dengan isolasi kegagalan (BUG-015/ADR-017).

        Satu batch gagal (DB down, constraint, deadlock) TIDAK boleh
        mematikan stream — exception ditangkap, di-log terstruktur
        rate-limited (`ingest_batch_error`), dan stream lanjut ke tx
        berikutnya. Batch yang gagal hilang untuk sementara; backfill
        Data API dan idempotensi `insert_many` menutup gap saat jalur
        pulih. Sebelum fix, exception ini naik sampai `run()` →
        `ingest_stream_error` berulang + `PendingRollbackError` menular
        ke semua batch berikutnya.
        """
        try:
            saved = await self._trades.insert_many(trades)
        except Exception as exc:
            self._batch_errors += 1
            if self._batch_errors == 1 or self._batch_errors % 10 == 0:
                # Rate-limit: batch gagal biasanya beruntun (DB down) —
                # jangan banjiri journald; penghitung tetap naik tiap gagal
                # dan muncul di heartbeat `ingest_heartbeat`.
                logger.error(
                    "gagal menyimpan batch trade; stream dilanjutkan",
                    extra={
                        "event": "ingest_batch_error",
                        "error": type(exc).__name__,
                        "trades": len(trades),
                        "batch_errors": self._batch_errors,
                        "tx_hash": trades[0].tx_hash,
                    },
                )
            await self._heartbeat(ok=False)
            return 0
        stats.trades_saved += saved
        self._total_saved += saved
        return saved

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
