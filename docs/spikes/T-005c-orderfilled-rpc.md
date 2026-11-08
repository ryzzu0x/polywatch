# T-005c — Spike OrderFilled → wallet via RPC Polygon

- **Tanggal:** 2026-11-08
- **Status:** Selesai — decode TERBUKTI, lanjut T-011
- **Script probe:** `scripts/spike/t005c_orderfilled_probe.py` (throwaway, bisa dijalankan ulang, tanpa secret di repo)
- **Artefak run:** `t005c-run.json` (folder ini; 39 trade, 4 market updown 5m)

## Tujuan

Membuktikan dengan data nyata bahwa event WS CLOB (`last_trade_price`, median ~81 ms tapi **tanpa alamat wallet**, lihat T-005b) bisa ditukar menjadi detail transaksi lengkap — **alamat wallet** — lewat `eth_getTransactionReceipt` RPC Polygon, dan mengukur latency end-to-end (event WS diterima → trade ter-decode lengkap).

## Metode

1. Cari market Up/Down live via Gamma (`closed=false&order=endDate&ascending=true&end_date_min=<now-1h>`, paginasi offset — market updown muncul di offset ~500-700, bukan halaman pertama).
2. Subscribe WS market channel `wss://ws-subscriptions-clob.polymarket.com/ws/market` untuk `clobTokenIds` 4 market (8 token).
3. Setiap event `last_trade_price`: catat waktu terima lokal → `eth_getTransactionReceipt(tx_hash)` (RPC, poll 250 ms) → telusuri `logs` → decode log event → buat catatan "tersimpan" (struktur Trade lengkap: wallet, token id, size, price, arah). E2E = waktu terima → selesai decode.
4. Signature event diverifikasi dua jalur independen: keccak-256 kandidat signature + database signature openchain.xyz.

## Temuan utama

### 1. Kontrak exchange & event yang benar (empiris, bukan asumsi)

**Seluruh tx market Up/Down yang diamati (39/39, 4 market 5m: sol/xrp/zec/doge) tereksekusi di satu kontrak: CTF Exchange V2.**

| Item | Nilai |
|---|---|
| Kontrak (dari `tx.to` + address log) | `0xE111180000d2663C0091e4f400237545B87B996B` (CTF Exchange V2) |
| Event | `OrderFilled` |
| Signature lengkap (terverifikasi openchain + repo Polymarket ctf-exchange-v2) | `OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)` |
| topic0 | `0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee` |

Perbandingan kontrak (dari repo Polymarket, diverifikasi empiris di tx nyata):

- **CTF Exchange V1** (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`): signature V1 `OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)` (topic0 `0x0879b055...`) **tidak pernah muncul** di 39 tx yang diamati.
- **Neg Risk CTF Exchange** (`0xE2222d279d744050d28E00520010520000310F59`): **tidak muncul sama sekali** di market Up/Down (sesuai dokumentasi: neg-risk market terpisah; updown memakai exchange standar).
- **CTF Exchange V2** (alamat di atas): muncul di 100% tx. Event pendamping yang juga muncul di log yang sama: `OrdersMatched` (topic0 `0x174b3811...`) dan `FeeCharged` (`0x55bb3cad...`).

Event V2 (perbedaan penting vs V1, dari `Events.sol` ctf-exchange-v2): data non-indexed = `(side: uint8, tokenId: uint256, makerAmountFilled: uint256, takerAmountFilled: uint256, fee: uint256, builder: bytes32, metadata: bytes32)` — 7 word statis. **`makerAssetId`/`takerAssetId` V1 hilang**; digantikan `side` + `tokenId` tunggal.

### 2. Interpretasi field (dari source kontrak + validasi silang 39 tx)

- `topics[1]` = orderHash, `topics[2]` = **maker** (pemilik order, wallet nyata), `topics[3]` = **taker**.
- `side=0` (BUY): maker menjual USDC → `makerAmountFilled` = USDC, `takerAmountFilled` = jumlah outcome token.
- `side=1` (SELL): maker menjual outcome token → `makerAmountFilled` = token, `takerAmountFilled` = USDC.
- Semua amount 6 desimal (USDC & ERC-1155 outcome).
- `price = usdc / size`.
- **Satu tx bisa memuat >1 log OrderFilled** (2-5 log per tx di sampel; match order operator vs beberapa maker order). Wallet yang dicatat Polymarket/Data API adalah **maker** tiap fill (dan operator untuk fill "facing exchange" dengan taker=alamat exchange).
- Validasi silang event WS ↔ decode: `tokenId` decode == `asset_id` WS, `size` == WS `size`, `price` == WS `price` (presisi penuh), `side` konsisten — 39/39 cocok.

### 3. Contoh tx ter-decode (nyata)

```
tx_hash: 0x7c55f9c66fa0ba9de570e3de1fae0be5656031433cea0143a041c1cdfdf6440c
market : sol-updown-15m-1794122100 (token 78130...8275782 = token Down)
WS event: price=0.12 size=8.333334 side=BUY

log[850] OrderFilled: maker=0xd6d2b81f0c60e358500a84ce8fbd566bbcc73f40
         taker=0x59178ed0f66bcfa02193bcc77097715399bedb33 side=SELL(1)
         size=8.333334 token, usdc=1.0 → price 0.12
log[852] OrderFilled: maker=0x59178ed0f66bcfa02193bcc77097715399bedb33
         taker=0xe11118...b996b (exchange) side=BUY(0)
         size=8.333334 token, usdc=1.0, fee=61600
```

### 4. Latency end-to-end (39 trade, ≥20 diminta)

| Metrik | Nilai |
|---|---|
| **E2E median (WS diterima → ter-decode/tersimpan)** | **855 ms** |
| E2E p90 | 1.683 ms |
| E2E max | 2.128 ms |
| Latency event WS sendiri (recv − timestamp) | median 424 ms, p90 1.336 ms (lebih tinggi dari T-005b ~81 ms; probe ini mem-*blocking* receipt di loop recv sehingga event menumpuk — latency WS sebenarnya tetap yang T-005b) |
| RPC receipt per panggilan (Alchemy) | median 7,5 ms |
| **Banding Data API v2 (T-005)** | median **164 s** → **~190× lebih cepat** |

Penting: **event WS datang PRA-KONFIRMASI**. Panggilan receipt pertama (7-9 ms setelah event) mengembalikan `result: null` — tx belum masuk blok. Waktu dominan e2e adalah menunggu tx masuk blok Polygon (~2 s block time). Poll 250 ms menutup gap itu. Jadi arsitektur: WS → poll receipt → decode. Tidak perlu `eth_getLogs` (receipt sudah memuat semua log tx; `eth_getLogs` per-block malah lebih lambat untuk kasus tx-tunggal).

### 5. Uji endpoint RPC (eth_getTransactionReceipt, 15 panggilan/endpoint)

| Endpoint | median | p90 | max | Status |
|---|---|---|---|---|
| Alchemy (`ALCHEMY_RPC_URL`) | **7,5 ms** | 18,3 ms | 18,5 ms | tercepat, dipakai sebagai primer |
| polygon.drpc.org | 15,9 ms | 35,6 ms | 48,8 ms | cadangan terbaik keyless |
| publicnode | 186,4 ms | 199,2 ms | 207,0 ms | hidup, lebih lambat |
| 1rpc.io/matic | 269,3 ms | 513,4 ms | 649,8 ms | hidup, paling lambat |

Failover urutan yang dipakai T-011: **Alchemy → publicnode → 1rpc → drpc** (sesuai instruksi; drpc tercepat keyless tapi urutan failover hanya menentukan pemulihan setelah primer gagal).

### 6. Heartbeat & reconnect

Terkonfirmasi ulang T-005b: server menutup koneksi dengan close code **1000 "all subscribed assets resolved"** saat semua market dalam satu siklus selesai (run ini: setelah ~400 s). Reconnect + re-subscribe token market live baru WAJIB tiap siklus. Text-frame `PING`/`PONG` tetap bekerja.

## Kesimpulan tegas

- **Decode TERBUKTI.** `OrderFilled` CTF Exchange V2 memuat maker/taker wallet, tokenId, jumlah, side, fee — semua yang dibutuhkan `Trade` domain. Mapping token id → market/outcome via `markets.token_up`/`token_down` langsung cocok.
- **Latency: CUKUP BANGET.** E2E median 855 ms (p90 1,7 s) — jauh di bawah target 10 s dan ~190× lebih cepat dari Data API (164 s).
- **Cukup `eth_getTransactionReceipt`** — tidak perlu `eth_getLogs`, tidak perlu decoder ABI (layout statis 7 word, slicing manual cukup, tanpa dependensi berat).
- Trade-off vs Data API: WS+RPC memberi **semua fill per tx** (termasuk wallet operator & maker per fill) — lebih granular daripada agregat Data API, dan jauh lebih cepat.

## Batasan

- 39 trade dari 4 market 5m pada satu run (satu jam sibuk); pasar sepi bisa berbeda tapi margin ke target 10 s sangat besar.
- Market non-updown (neg-risk, sport) bisa memakai kontrak exchange lain (Neg Risk) — decoder perlu tabel kontrak bila scope diperluas; untuk MVP updown hanya V2.
- Probe mem-*blocking*-kan receipt di loop WS recv → latency WS tampak 424 ms (distorsi; T-005b tetap acuan latency WS murni). Implementasi T-011 memisahkan task recv dan task RPC.
- Fee tidak didekode ke biaya per wallet (fee diambil operator, tidak mengurangi jumlah maker/taker yang tercatat di log).
- Tidak mengukur latency `backfill` Data API (sudah diukur T-005: 164 s).

## Rekomendasi (masuk T-011)

1. Ingest hybrid: WS `last_trade_price` → poll receipt (interval 250 ms, timeout 10 s) → decode `OrderFilled` V2 → simpan idempoten.
2. Failover RPC: Alchemy (bila ada) → publicnode → 1rpc → drpc; label host saja di log.
3. Dedupe: kunci `(tx_hash, log_index, wallet)` — unik di DB, plus cache in-memory tx_hash yang sedang diproses.
4. Backfill Data API tetap dipertahankan untuk gap (WS down / market resolve sebelum sempat).
