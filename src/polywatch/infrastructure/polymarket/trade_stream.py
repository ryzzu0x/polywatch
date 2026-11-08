"""Adapter trade real-time: CLOB WS + RPC Polygon → port application (T-011).

- `OrderbookWsAdapter` membungkus `OrderbookWsClient` menjadi
  `TradeEventStream` (event `last_trade_price`).
- `PolygonReceiptAdapter` membungkus `PolygonRpcClient` + decoder
  `OrderFilled` menjadi `ReceiptFetcher`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from polywatch.application.dto.trade import FilledOrderSnapshot, TradeEventSnapshot
from polywatch.infrastructure.polymarket.order_filled_decoder import (
    decode_order_filled,
    order_filled_logs,
)
from polywatch.infrastructure.polymarket.orderbook_ws import (
    LastTradeEvent,
    OrderbookWsClient,
)
from polywatch.infrastructure.polymarket.polygon_rpc import PolygonRpcClient


class OrderbookWsAdapter:
    """`TradeEventStream` dari klien WS CLOB market channel."""

    def __init__(self, client: OrderbookWsClient | None = None) -> None:
        self._client = client or OrderbookWsClient()

    def stream(self, token_ids: Sequence[str]) -> AsyncIterator[TradeEventSnapshot]:
        """Stream event trade untuk token; reconnect ditangani klien."""
        return self._stream(list(token_ids))

    async def _stream(self, token_ids: list[str]) -> AsyncIterator[TradeEventSnapshot]:
        async for event in self._client.events(token_ids):
            yield _to_snapshot(event)


def _to_snapshot(event: LastTradeEvent) -> TradeEventSnapshot:
    return TradeEventSnapshot(
        tx_hash=event.tx_hash,
        asset_id=event.asset_id,
        condition_id=event.condition_id,
        timestamp_ms=event.timestamp_ms,
    )


class PolygonReceiptAdapter:
    """`ReceiptFetcher`: receipt RPC → daftar fill ter-decode."""

    def __init__(self, rpc: PolygonRpcClient) -> None:
        self._rpc = rpc

    async def filled_orders(self, tx_hash: str) -> Sequence[FilledOrderSnapshot] | None:
        """Fill `OrderFilled` di tx; None bila receipt tak tersedia (timeout)."""
        receipt = await self._rpc.get_transaction_receipt(tx_hash)
        if receipt is None:
            return None
        snapshots: list[FilledOrderSnapshot] = []
        for log in order_filled_logs(receipt):
            filled = decode_order_filled(log)
            snapshots.append(
                FilledOrderSnapshot(
                    tx_hash=tx_hash,
                    log_index=filled.log_index,
                    maker=filled.maker,
                    taker=filled.taker,
                    side=filled.side,
                    token_id=filled.token_id,
                    size=filled.size,
                    usdc=filled.usdc,
                    fee=filled.fee,
                )
            )
        return snapshots
