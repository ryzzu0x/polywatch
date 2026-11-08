# T-005 — Spike Sumber Data Polymarket

- **Tanggal:** 2026-11-08
- **Status:** Selesai
- **Script probe:** `scripts/spike/t005_probe.py` (throwaway, bisa dijalankan ulang, tanpa secret)
- **Contoh respons:** `t005-gamma-market-sample.json`, `t005-data-api-trade-sample.json`, `t005-crypto-price-sample.json`, `t005-measurements.json` (folder ini)

## Tujuan

Membuktikan dengan pengukuran nyata: (a) cara mengambil market Up/Down 5m/15m, (b) cara mengambil trade per market beserta latency & rate limit, (c) sumber harga open/close yang paling cocok dengan resolusi resmi.

## Metode

Semua angka berasal dari eksekusi nyata `scripts/spike/t005_probe.py` + probe manual (curl/Python http.client) tanggal 2026-11-08 sekitar 03:20–04:30 UTC. Rate limit diuji sopan (10 request berurutan tanpa jeda + jeda antar batch), bukan hammering.

## Temuan utama (mengubah asumsi dokumen)

1. **Data API v1 SUDAH PENSIUN.** `https://data-api.polymarket.com/trades` mengembalikan **410 Gone** dengan pesan `"Data API v1 has been retired"` dan migrasi ke `https://data-api.polymarket.com/v2/`. Endpoint yang dipakai `docs/ARCHITECTURE.md` §7 harus diubah ke v2.
2. **Parameter filter berubah.** Di v2, trade per market difilter dengan `condition=<conditionId>` (bukan `market=` seperti v1; `market` ditolak 400). `token_id` diterima tapi **diabaikan** (mengembalikan firehose global — diverifikasi: 1 request `token_id=X` mengembalikan 33 token_id berbeda). Di v1, filter `market` adalah conditionId.
3. **Pagination v2 cursor-only.** `offset` ditolak 400: `"pages are cursor-only, pass 'cursor' from a prior response's next_cursor"`. `limit` per halaman hingga 500 diverifikasi bekerja.
4. **Gamma: market updown resolved bisa diambil**, tapi TIDAK lewat `closed=true` tanpa filter (halaman 1 berisi market jangka panjang). Pola yang terbukti: `closed=true&order=endDate&ascending=false&end_date_max=<ISO>` mengembalikan batch updown yang baru resolve (96 market dalam 2 halaman). Sebaliknya, `closed=false&order=endDate&ascending=true&end_date_min=<ISO>` menemukan market live (36 market).
5. **Gamma menghapus market live dari pencarian slug setelah end.** `/markets?slug=X` → `[]` beberapa menit setelah endTime; query yang sama dengan `&closed=true` tetap mengembalikan market + hasil resolusi. Menyusun slug dari unix time tetap valid (pola slug `{aset}-updown-{5m|15m}-{unix_start}` terverifikasi di 96 market, 8 aset: bnb, btc, doge, eth, hype, sol, xrp, zec — plus xag/xau pada run lain).
6. **`volume` di Gamma null untuk market live**, terisi setelah resolve (contoh: `"42.068629"`).
7. **Resolusi tidak instan.** Contoh nyata `btc-updown-5m-1794110400` (end 04:05:00Z): `closed=true` baru mengembalikan hasil pada 04:10:43Z → **delay ±343 detik** (~5–6 menit) setelah end. Polling hasil harus toleran delay ini.
8. **Resolusi resmi tersedia dari Gamma:** `closed=true` + `umaResolutionStatus="resolved"` + `outcomePrices=["1","0"]` (indeks 0 = Up). Endpoint `/trades` Data API tidak dipakai untuk resolusi.
9. **Sumber harga selaras resolusi (temuan penting ADR-005):** Polymarket sendiri mengekspos `GET polymarket.com/api/crypto/crypto-price?symbol=btc&eventStartTime=<unix_window_start>` → `{"openPrice":..., "closePrice":..., "timestamp":...}` — harga **Chainlink yang sama dipakai resolusi** (TWAP stream). Diverifikasi: `openPrice` konstan sepanjang window aktif dan berganti tepat saat window baru mulai (75787 → 75918.01 pada pergantian window); `closePrice` live. **Batasan:** parameter `eventStartTime` diabaikan saat ini (historical window tidak bisa direplay — selalu window aktif yang dikembalikan), jadi open harus dicatat saat window mulai dan close saat window berakhir, tidak bisa di-backfill lewat endpoint ini. Web data.chain.link diblokir bot-checkpoint (429/000); Chainlink Data Streams resmi butuh API key → kandidat Chainlink langsung tidak terukur tanpa kredensial.

## Angka hasil ukur

### Gamma API

| Ukuran | Nilai | Catatan |
|---|---|---|
| Endpoint daftar market | `GET /markets?closed=&order=endDate&ascending=&end_date_max=` | bekerja terbukti |
| Market live updown ditemukan | 36 (3 halaman) | order endDate asc + end_date_min |
| Market resolved ditemukan | 96 (2 halaman) | closed=true + end_date_max |
| Market per jam | 5m: ±12/jam per aset aktif; total 5m+15m ≈ 24/jam per window; 8–10 aset aktif | dari 96 resolved: 72 5m + 24 15m ≈ 6 jam histori |
| Latency median GET /markets | 0.208–0.238 s | httpx, 6 halaman |
| Rate limit | 10 request berurutan → semua 200; tidak ada header `x-ratelimit-*` | uji sopan |
| Aman dianggap | ≤5 req/s dengan jeda; tidak ada sinyal limit pada 10 burst | tidak diuji sampai kena limit (sengaja, jangan DoS) |

### Data API v2 (trades)

| Ukuran | Nilai | Catatan |
|---|---|---|
| Endpoint | `GET /v2/trades?condition=<conditionId>&limit=500` | `market`/`offset` ditolak 400 |
| Pagination | cursor (`next_cursor`), `limit` max 500/halaman | offset tidak didukung |
| Trade per market | contoh `sol-5m`: 582 trade, 2 halaman | market 5m ramai |
| Field per trade | `proxy_wallet`, `side`, `token_id`, `condition_id`, `size`, `price`, `timestamp`, `slug`, `outcome`, `outcome_index`, `transaction_hash`, `name`, `pseudonym`, `title`, `icon`, dll. | lengkap untuk kebutuhan skor wallet |
| Duplikat | polling berulang mengembalikan baris sama (1400/1500 rows duplikat) → insert harus idempoten (upsert by `transaction_hash`+`token_id`+`price`+`size`+`timestamp`) | |
| **Latency trade→response** | **median 164 s, p90 251 s** (82 trade baru, market live sol-5m, poll tiap 4 s) | terukur pada window live; run pertama di market yang sudah selesai memberi median 146 s — konsisten: **Data API bukan sumber real-time** |
| Rate limit | 10 request berurutan → semua 200; tidak ada header `x-ratelimit-*` | |

### Resolusi & harga open/close

| Kandidat | Akurasi vs resolusi resmi | Catatan |
|---|---|---|
| Polymarket `crypto-price` (Chainlink selaras) | **3/4 window live cocok (75%)** — 1/1 window pertama + 2/3 window berikutnya | `openPrice` kadang **stale** saat pergantian window (konstan 75918.01 selama 3 window pada sampel) → mismatch terjadi ketika open resmi berbeda dari openPrice yang dikembalikan; hanya window aktif, tidak bisa backfill |
| Binance 1m klines (open/close spot window) | **41/48 cocok (85,4%)** | 48 market resolved (btc/eth/sol/xrp, 5m+15m) |
| Binance 1m TWAP menit vs open | **41/48 cocok (85,4%)** | sama; 2 market beda prediksi vs spot |
| Chainlink Data Streams langsung | **tidak terukur** — butuh API key; web UI diblokir bot-checkpoint | |

Catatan penting akurasi: sampel `crypto-price` kecil (n=4 window live) dan menunjukkan bug/perilaku `openPrice` stale antar window. Jumlah mismatch: window 1794111000 — openPrice endpoint 75918.01, closePrice 75920.97 → pred UP; resolusi resmi DOWN. Karena `eventStartTime` diabaikan, tidak bisa divalidasi nilai open yang benar-benar dipakai resolusi secara retroaktif.

Selisih harga median pada 7 mismatch Binance: **US$0,03** (BTC/SOL/XRP) — mismatch bukan karena gap besar tapi karena TWAP Chainlink ≠ spot Binance (contoh: btc-1794108900 resmi DOWN padahal Binance spot naik 75853.09→75869.94; odds market saat live memang 0.0005/0.9995 → resolusi DOWN benar per Chainlink, spot Binance salah arah).

## Rekomendasi

1. **Sumber trade (ADR-004):** polling `GET data-api.polymarket.com/v2/trades?condition=<conditionId>&limit=500` (cursor pagination, insert idempoten). Latency median 164 s / p90 251 s jauh dari target delay ≤10 s → sesuai rencana ADR-004, event on-chain (OrderFilled) dipertimbangkan di v1. Rate limit aman: tidak ada header limit, 10 burst 200 semua; pakai ≤5 req/s + backoff.
2. **Sumber open/close (ADR-005):** **utama = Polymarket `crypto-price`** (Chainlink selaras resolusi): catat `openPrice` saat window mulai, `closePrice` saat window berakhir; `price_source="polymarket-chainlink"`. Hati-hati: `openPrice` terpantau stale pada satu pergantian window (3/4 cocok pada sampel live kecil) → **cadangan = Binance 1m klines** (akurasi 85,4% terhadap resolusi, tanpa kunci; untuk tampilan/volatilitas dan cross-check arah). Resolusi resmi selalu diambil dari Gamma (`closed=true` + `outcomePrices`), harga hanya informasi — sesuai konteks ADR-005. Karena n sampel crypto-price masih kecil, producer harus mencatat kedua sumber (`price_source`) dan audit akurasi dilakukan otomatis saat berjalan; keputusan dievaluasi ulang di v1 bila akurasi crypto-price < Binance.
3. **Discovery (T-007):** polling Gamma `/markets?closed=false&order=endDate&ascending=true&end_date_min=<now-1j>` tiap ~30 s untuk market live baru; `/markets?closed=true&...&end_date_max=<now>` untuk hasil resolusi (toleransi delay ±6 menit).

## Batasan

- Rate limit tidak diuji sampai kena limit (429) — sengaja, untuk tidak men-DoS API publik. Angka "aman" adalah lower bound.
- Akurasi `crypto-price` baru diverifikasi penuh pada 1 window live (Binance pada 48 market) — n perlu ditambah saat producer berjalan; kolom `price_source` memungkinkan audit retroaktif.
- Latency Data API diukur pada 1 market live ramai (sol 5m); pasar sepi bisa berbeda.
- CLOB API (`clob.polymarket.com/trades`) menolak 401 tanpa API key — tidak terukur di spike ini; orderbook harga MVP diambil via endpoint publik `/book?token_id=` (belum diukur, di luar scope T-005).
- RPC publik Polygon (drpc free) menolak `eth_getLogs` range >10rb blok & ConditionalTokens event untuk cid sampel tidak ditemukan di rentang dibatasi → resolusi on-chain tidak terukur; resolusi resmi cukup diambil dari Gamma.
