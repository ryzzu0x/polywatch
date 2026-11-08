"""Klien JSON-RPC Polygon dengan failover endpoint berurutan.

Urutan failover (T-011): `ALCHEMY_RPC_URL` (bila di-set) → publicnode →
1rpc → drpc. Setiap panggilan `eth_getTransactionReceipt` diulang sampai
tx terkonfirmasi (event WS datang *pra-konfirmasi*: receipt `null` sampai
tx masuk blok — temuan T-005c) dengan interval pendek; endpoint gagal
(timeout/transport/JSON-RPC error) dirotasi ke endpoint berikutnya.

URL ber-key (Alchemy) hanya diterima lewat parameter — tidak pernah
dicetak ke log (JsonFormatter menyamarkan `*api_key*`/`*token*`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PUBLIC_ENDPOINTS: tuple[str, ...] = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
)
RECEIPT_POLL_INTERVAL_S = 0.25
RECEIPT_TIMEOUT_S = 10.0
RPC_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class RpcUnavailableError(RuntimeError):
    """Semua endpoint RPC gagal untuk satu permintaan."""


class PolygonRpcClient:
    """Klien JSON-RPC async dengan failover endpoint + poll receipt."""

    def __init__(
        self,
        endpoints: list[str] | None = None,
        alchemy_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        ordered: list[str] = []
        if alchemy_url:
            ordered.append(alchemy_url)
        if endpoints is not None:
            ordered.extend(endpoints)
        else:
            ordered.extend(PUBLIC_ENDPOINTS)
        if not ordered:
            raise ValueError("minimal satu endpoint RPC diperlukan")
        self._endpoints = ordered
        self._index = 0
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=RPC_TIMEOUT)

    @property
    def endpoint_count(self) -> int:
        return len(self._endpoints)

    def _label(self, url: str) -> str:
        """Label endpoint untuk log tanpa membocorkan key (host saja)."""
        try:
            host = url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            host = "unknown"
        return host

    async def _post(self, method: str, params: list[Any]) -> dict[str, Any]:
        """POST JSON-RPC ke endpoint aktif; rotasi endpoint saat gagal."""
        last_error: Exception | None = None
        for _ in range(len(self._endpoints)):
            url = self._endpoints[self._index]
            try:
                response = await self._client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                body = response.json()
                if isinstance(body, dict) and body.get("error") is not None:
                    raise ValueError(f"JSON-RPC error: {body['error']}")
                if not isinstance(body, dict):
                    raise ValueError(f"respons bukan objek JSON-RPC: {type(body).__name__}")
                return body
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "rpc endpoint gagal, rotasi",
                    extra={"endpoint": self._label(url), "method": method, "error": str(exc)[:150]},
                )
                self._index = (self._index + 1) % len(self._endpoints)
        raise RpcUnavailableError(f"semua {len(self._endpoints)} endpoint gagal") from last_error

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """Ambil receipt; None bila tx belum masuk blok setelah timeout."""
        deadline = asyncio.get_running_loop().time() + RECEIPT_TIMEOUT_S
        while True:
            body = await self._post("eth_getTransactionReceipt", [tx_hash])
            result = body.get("result")
            if result is not None:
                return dict(result)
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "receipt belum tersedia sampai timeout (tx pra-konfirmasi?)",
                    extra={"tx_hash": tx_hash},
                )
                return None
            await asyncio.sleep(RECEIPT_POLL_INTERVAL_S)

    async def close(self) -> None:
        """Tutup klien internal (no-op bila klien disuntik pemanggil)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PolygonRpcClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
