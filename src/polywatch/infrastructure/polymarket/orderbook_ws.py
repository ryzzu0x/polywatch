"""Klien WebSocket CLOB Polymarket market channel (adapter T-011).

Subscribe token Up/Down market live; terima event `last_trade_price`
(memuat `transaction_hash`) — wallet diambil terpisah via RPC (T-005c).

Perilaku terukur (T-005b/T-005c):
- Server menutup koneksi dengan close code 1000
  `"all subscribed assets resolved"` tiap siklus market (5m/15m) →
  **reconnect + re-subscribe otomatis** dengan backoff.
- Heartbeat text-frame `PING` → dijawab `PONG`; koneksi idle >60 s tidak
  diputus server (tetap dikirim sebagai keepalive).
- Pesan bisa berupa satu objek (event tunggal) atau list objek.
- Event `price_change` sangat deras (~200/s per 2 market ramai) —
  hanya `last_trade_price` yang diteruskan ke queue.

Penggunaan: `async for trade_event in client.events(tokens)` —
generator objek `LastTradeEvent` (tx_hash + field event WS).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import websockets

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 15.0
RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0
RECV_TIMEOUT_S = 60.0
#: Toleransi jumlah token berubah sebelum wajib re-subscribe (T-026):
#: daftar token market live berubah tiap jendela 5m/15m; perbedaan
#: kecil (market selesai) tidak memicu re-subscribe langsung — market
#: yang resolve ditutup server dengan close 1000 dan memicu siklus baru.
RESUBSCRIBE_DELTA = 0


@dataclass(frozen=True, slots=True)
class LastTradeEvent:
    """Event `last_trade_price` CLOB WS — sumber tx_hash trade."""

    tx_hash: str
    asset_id: str
    price: str
    size: str
    side: str
    timestamp_ms: int
    condition_id: str


def parse_last_trade_events(raw: str) -> list[LastTradeEvent]:
    """Parse satu frame WS menjadi daftar event `last_trade_price`.

    Frame bisa berupa objek tunggal atau list objek; frame non-JSON
    (mis. `PONG`) dan event lain diabaikan. Raise ValueError bila JSON
    rusak (dipanggil di context yang menoleransi — pemanggil log & skip).
    """
    payload: Any = json.loads(raw)
    items = payload if isinstance(payload, list) else [payload]
    events: list[LastTradeEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("event_type") != "last_trade_price":
            continue
        tx_hash = item.get("transaction_hash")
        asset_id = item.get("asset_id")
        if not isinstance(tx_hash, str) or not isinstance(asset_id, str):
            continue
        ts = item.get("timestamp")
        timestamp_ms = int(ts) if isinstance(ts, (int, str)) and str(ts).isdigit() else 0
        events.append(
            LastTradeEvent(
                tx_hash=tx_hash,
                asset_id=asset_id,
                price=str(item.get("price", "")),
                size=str(item.get("size", "")),
                side=str(item.get("side", "")),
                timestamp_ms=timestamp_ms,
                condition_id=str(item.get("market", "")),
            )
        )
    return events


class OrderbookWsClient:
    """Klien WS market channel dengan reconnect + heartbeat otomatis.

    T-026: generator `events()` TIDAK berhenti saat server menutup
    koneksi (1000 "all subscribed assets resolved" tiap akhir jendela
    5m/15m). Setiap koneksi baru memakai daftar token terbaru yang
    di-set lewat `resubscribe(tokens)` — pemanggil cukup memanggilnya
    sekali per siklus; generator tetap hidup dan berganti daftar token
    pada koneksi berikutnya. `stop()` mengakhiri generator.
    """

    def __init__(
        self,
        url: str = CLOB_WS_URL,
        ping_interval_s: float = PING_INTERVAL_S,
        reconnect_base_delay_s: float = RECONNECT_BASE_DELAY_S,
    ) -> None:
        self._url = url
        self._ping_interval_s = ping_interval_s
        self._reconnect_base_delay_s = reconnect_base_delay_s
        self._stopping = asyncio.Event()
        self._tokens: list[str] = []
        self._tokens_changed = asyncio.Event()
        self.reconnects = 0
        self.subscribed_counts: list[int] = []

    def stop(self) -> None:
        """Minta generator berhenti setelah frame saat ini."""
        self._stopping.set()
        self._tokens_changed.set()  # bangunkan generator yang menunggu token

    def resubscribe(self, tokens: Sequence[str]) -> None:
        """Set daftar token untuk koneksi berikutnya (sinkron, non-blocking).

        Dipanggil pemanggil tiap siklus (token market live terbaru).
        Generator memakai daftar ini saat membuka koneksi baru; bila
        koneksi sedang hidup dan daftar berubah, koneksi di-restart agar
        token baru ikut di-subscribe (server tidak mendukung subscribe
        inkremental pada satu koneksi market channel).
        """
        new_tokens = list(tokens)
        if new_tokens == self._tokens:
            return
        self._tokens = new_tokens
        self._tokens_changed.set()

    async def events(self, tokens: Sequence[str]) -> AsyncIterator[LastTradeEvent]:
        """Stream `last_trade_price` untuk `tokens` sampai `stop()`.

        Koneksi ditutup server (1000 "all subscribed assets resolved") saat
        semua market resolve — generator membuka koneksi baru dengan daftar
        token terbaru (dari `resubscribe` atau argumen awal) dan melanjutkan
        stream. Hanya `stop()` yang mengakhiri generator.
        """
        if not self._tokens:
            self._tokens = list(tokens)
        attempt = 0
        while not self._stopping.is_set():
            if not self._tokens:
                # belum ada token live — tunggu `resubscribe()` pertama
                self._tokens_changed.clear()
                await self._tokens_changed.wait()
                continue
            try:
                async with websockets.connect(self._url, ping_interval=20, open_timeout=30) as ws:
                    await ws.send(json.dumps({"assets_ids": list(self._tokens), "type": "market"}))
                    attempt = 0
                    self.subscribed_counts.append(len(self._tokens))
                    logger.info(
                        "ws subscribed",
                        extra={"url": self._url, "tokens": len(self._tokens)},
                    )
                    async for event in self._stream(ws):
                        yield event
                    # server menutup koneksi dengan rapi (close 1000) —
                    # lanjut ke koneksi berikutnya tanpa backoff.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping.is_set():
                    break
                self.reconnects += 1
                attempt += 1
                delay = min(
                    self._reconnect_base_delay_s * (2 ** (attempt - 1)),
                    RECONNECT_MAX_DELAY_S,
                )
                logger.warning(
                    "ws putus, reconnect",
                    extra={
                        "error": str(exc)[:150],
                        "attempt": attempt,
                        "delay_s": delay,
                        "reconnects": self.reconnects,
                    },
                )
                await asyncio.sleep(delay)

    async def _stream(self, ws: websockets.ClientConnection) -> AsyncIterator[LastTradeEvent]:
        """Baca frame sampai koneksi tutup; kirim PING berkala."""
        ping_task = asyncio.create_task(self._heartbeat(ws))
        try:
            while True:
                if self._stopping.is_set():
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                if not isinstance(raw, str) or raw in ("PING", "PONG"):
                    continue
                try:
                    events = parse_last_trade_events(raw)
                except ValueError:
                    logger.warning("frame ws tidak terparse", extra={"raw": raw[:120]})
                    continue
                for event in events:
                    yield event
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ping_task

    async def _heartbeat(self, ws: websockets.ClientConnection) -> None:
        """Kirim text-frame PONG protokol Polymarket tiap interval."""
        while True:
            await asyncio.sleep(self._ping_interval_s)
            try:
                await ws.send("PING")
            except Exception:
                return
