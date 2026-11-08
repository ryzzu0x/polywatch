"""Decoder event `OrderFilled` CTF Exchange Polymarket dari log receipt.

Temuan empiris spike T-005c (2026-11-08): seluruh market Up/Down yang
diamati tereksekusi di **CTF Exchange V2** `0xe111180000d2663c0091e4f400237545b87b996b`
(bukan V1 `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` maupun Neg Risk
`0xC5d563A36AE78145C45a50134d48A1215220f80a`) — event `OrdersMatched`
(v1) tidak pernah muncul; yang muncul hanya `OrderFilled`.

Signature (terverifikasi keccak + openchain signature-database):
`OrderFilled(bytes32 indexed orderHash, address indexed maker,
address indexed taker, uint8 side, uint256 tokenId,
uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee,
bytes32 builder, bytes32 metadata)` — topic0
`0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee`.

Layout data (ABI static, 7 word):
- word0: `side` (0 = BUY, 1 = SELL — enum `Side` kontrak)
- word1: `tokenId` (ERC-1155 outcome token; 6 desimal)
- word2: `makerAmountFilled` (6 desimal)
- word3: `takerAmountFilled` (6 desimal)
- word4: `fee`
- word5-6: `builder`, `metadata` (tidak dipakai)

Makna amount per `side` (dari mixin Trading.sol ctf-exchange-v2):
- BUY  (`side=0`): maker memberi USDC → `makerAmountFilled` = USDC,
  `takerAmountFilled` = jumlah outcome token.
- SELL (`side=1`): maker memberi outcome token → `makerAmountFilled`
  = jumlah token, `takerAmountFilled` = USDC.

Decode dilakukan manual (slicing 32-byte word) — tanpa library ABI berat.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

EXCHANGE_V2_ADDRESS = "0xe111180000d2663c0091e4f400237545b87b996b"
ORDER_FILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
TOKEN_DECIMALS = 10**6  # USDC & outcome token Polymarket: 6 desimal
_WORD = 32
_EXPECTED_DATA_BYTES = 7 * _WORD


class OrderFilledDecodeError(ValueError):
    """Log tidak sesuai layout `OrderFilled` CTF Exchange V2."""


@dataclass(frozen=True, slots=True)
class FilledOrder:
    """Satu log `OrderFilled` ter-decode (sudah dalam unit manusiawi)."""

    order_hash: str
    maker: str
    taker: str
    side: int  # 0 = BUY, 1 = SELL (enum Side kontrak)
    token_id: int
    size: Decimal  # jumlah outcome token
    usdc: Decimal  # nilai USDC
    fee: int
    log_index: int

    @property
    def price(self) -> Decimal:
        """Harga per outcome token; 0 bila size nol (guard divisi)."""
        return self.usdc / self.size if self.size > 0 else Decimal(0)


def _require_hex_topics(topics: list[str]) -> None:
    if len(topics) < 4:
        raise OrderFilledDecodeError(f"topik tidak lengkap: {len(topics)} (butuh >= 4)")
    for i, topic in enumerate(topics[:4]):
        if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
            raise OrderFilledDecodeError(f"topik[{i}] bukan bytes32 hex: {topic!r}")


def decode_order_filled(log: dict[str, object]) -> FilledOrder:
    """Decode satu objek log JSON-RPC `OrderFilled` menjadi `FilledOrder`.

    `log` adalah satu elemen `receipt["logs"]` apa adanya dari
    `eth_getTransactionReceipt`. Raise `OrderFilledDecodeError` bila topic0
    bukan OrderFilled atau layout data tidak cocok.
    """
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        raise OrderFilledDecodeError("log tanpa topics")
    if topics[0] != ORDER_FILLED_TOPIC:
        raise OrderFilledDecodeError(f"topic0 bukan OrderFilled: {topics[0]!r}")
    _require_hex_topics(topics)

    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise OrderFilledDecodeError(f"data bukan hex string: {data!r}")
    raw = bytes.fromhex(data[2:])
    if len(raw) != _EXPECTED_DATA_BYTES:
        raise OrderFilledDecodeError(
            f"panjang data {len(raw)} byte, harapan {_EXPECTED_DATA_BYTES}"
        )
    words = [int.from_bytes(raw[i * _WORD : (i + 1) * _WORD], "big") for i in range(5)]
    side, token_id, maker_amount, taker_amount, fee = words

    if side not in (0, 1):
        raise OrderFilledDecodeError(f"side tidak dikenal: {side}")
    if token_id == 0:
        raise OrderFilledDecodeError("tokenId nol (kolateral), bukan outcome token")

    if side == 0:  # BUY: makerAmount = USDC, takerAmount = token
        size_units, usdc_units = taker_amount, maker_amount
    else:  # SELL: makerAmount = token, takerAmount = USDC
        size_units, usdc_units = maker_amount, taker_amount

    log_index_raw = log.get("logIndex")
    if isinstance(log_index_raw, str):
        log_index = int(log_index_raw, 16)
    elif isinstance(log_index_raw, int):
        log_index = log_index_raw
    else:
        raise OrderFilledDecodeError(f"logIndex tidak valid: {log_index_raw!r}")

    return FilledOrder(
        order_hash=topics[1],
        maker="0x" + topics[2][-40:],
        taker="0x" + topics[3][-40:],
        side=side,
        token_id=token_id,
        size=Decimal(size_units) / TOKEN_DECIMALS,
        usdc=Decimal(usdc_units) / TOKEN_DECIMALS,
        fee=fee,
        log_index=log_index,
    )


def order_filled_logs(receipt: dict[str, object]) -> list[dict[str, object]]:
    """Ambil log `OrderFilled` dari receipt (dict JSON-RPC apa adanya)."""
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return []
    out: list[dict[str, object]] = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        if isinstance(topics, list) and topics and topics[0] == ORDER_FILLED_TOPIC:
            out.append(log)
    return out
