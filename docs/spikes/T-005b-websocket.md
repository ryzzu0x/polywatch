# T-005b — Spike CLOB WebSocket Market Channel

- **Tanggal:** 2026-11-08
- **Status:** Selesai
- **Script probe:** `scripts/spike/t005b_ws_probe.py` (throwaway, bisa dijalankan ulang, tanpa secret)
- **Artefak run:** `t005b-run-zec-btc.json`, `t005b-run-doge-zec.json`, `t005b-run-btc-bnb.json` (folder ini)

## Tujuan

Membuktikan dengan pengukuran nyata: (a) apakah CLOB WebSocket market channel bisa memberi event trade/harga dengan latency ≤ 10 detik, (b) apakah event-nya memuat identitas wallet (alamat), yang wajib untuk alert copy-trade per wallet.

## Metode

Sambung ke `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribe `{"assets_ids": [<clobTokenIds Up+Down market updown 5m live>], "type": "market"}` (bentuk ini terbukti diterima; varian `{"type":...,"assets_ids":...}` juga diterima, hasil sama). Market live dicari via Gamma `closed=false&order=endDate&ascending=true&end_date_min=<now-1h>`. Latency = waktu terima lokal − `timestamp` event (ms, string di semua event). Total 6 run (60–330 s), 4 run ≥ 5 menit bermakna; 2 run dengan `--compare-data-api` membandingkan `transaction_hash` WS vs Data API v2.

## Temuan utama

1. **Subscribe bekerja, event mengalir deras.** Event yang teramati: `book` (snapshot), `price_change` (dominan, ~80–42.000/run 5 mnt), `last_trade_price`, `tick_size_change`. Server meminta heartbeat: kirim text-frame `"PING"`, dijawab `"PONG"`; koneksi dengan `ping_interval=20` juga tetap hidup.
2. **`last_trade_price` memuat identitas trade TAPI TANPA alamat wallet.** Field lengkap: `asset_id`, `event_type`, `fee_rate_bps`, `market` (conditionId), `price`, `side`, `size`, `timestamp` (string ms), `transaction_hash`. **Tidak ada `proxy_wallet`/maker/taker address.**
3. **Latency event sangat rendah dan stabil (jauh di bawah target 10 s):**

| Run (market 5m, durasi) | Event n | Latency median | p90 | Max |
|---|---|---|---|---|
| zec+btc, 330 s | 28.735 | 82 ms | 83 ms | 436 ms |
| doge+zec, 330 s | 7.951 | 80 ms | 82 ms | 284 ms |
| btc+bnb, 330 s | 12.736 | 81 ms | 89 ms | 592 ms |
| **Gabungan (3 run ≥5 mnt)** | **49.422** | **~81 ms** | **~85 ms** | **592 ms** |

   Latency didominasi `price_change` (harga orderbook live). `last_trade_price` yang tersimpan sampel menunjukkan latency 82–84 ms.
4. **Trade WS ↔ Data API cocok satu-satu via `transaction_hash`.** Pada run btc+bnb: Data API mengembalikan 500 trade untuk market yang sama; 2 `last_trade_price` tersimpan (batas penyimpanan sampel 400 event pertama) — keduanya ditemukan di Data API dengan `proxy_wallet`, `side`, `size`, `price` sama. Jadi WS mengirim semua trade, hanya saja **tanpa alamat wallet**; wallet harus diambil dari jalur lain (Data API `/v2/trades?condition=` atau event on-chain OrderFilled yang memuat maker/taker).
5. **Server menutup koneksi saat semua token resolve**: close code **1000** dengan alasan `"all subscribed assets resolved"` — terjadi di hampir semua run 5 mnt (market 5m hidup hanya ≤5 menit). **Reconnect + re-subscribe wajib** tiap siklus market; tidak ada idle timeout yang teramati sebelum market resolve (idle >60 s tidak memutus). WS close code 1000 terlihat `null` di probe karena `async with` menangkap setelah loop; log `connect_log` mencatat `ConnectionClosedOK(Close(code=1000, reason='all subscribed assets resolved'))`.
6. **Batas token per koneksi tidak terukur** — hanya diuji 4 token (2 market) per koneksi. `price_change` bisa sangat deras (42.755 event / 3,5 mnt pada 2 market ramai → ~200 event/s) sehingga channel harga perlu difilter/ dibatasi kalau dipakai luas.

## Kesimpulan tegas

- **Latency: CUKUP.** Median ~81 ms, p90 ~85 ms, max < 600 ms — jauh di bawah target ≤ 10 s (vs polling Data API: median 164 s, p90 251 s).
- **Identitas wallet: TIDAK ADA.** `last_trade_price` membawa `transaction_hash`, `price`, `size`, `side`, `timestamp` tetapi **tidak membawa alamat wallet**. WS market channel **tidak cukup sendirian** untuk alert copy-trade per wallet.

## Rekomendasi

1. **Jangan ganti ingest trade MVP** (tetap polling Data API v2, ADR-004). WS tidak menghilangkan kebutuhan Data API karena wallet tidak tersedia di WS — dan `transaction_hash` WS bisa jadi *trigger* untuk fetch trade spesifik.
2. **Arsitektur hybrid untuk v1 (angka latency terukur):** WS market channel sebagai *detector* real-time (event `last_trade_price` + `price_change`, latency ~80 ms) → begitu `transaction_hash`/market aktif terlihat, segera poll `GET /v2/trades?condition=<cid>&limit=500` untuk mengambil `proxy_wallet` → evaluasi alert. Target ≤ 10 s realistis: deteksi ~0,1 s + satu poll Data API (latency GET terukur ~0,2 s di T-005) — delay total ditentukan kapan Data API membuat trade ter-sebut tersedia (median 164 s di T-005, tapi trade yang baru saja terjadi cenderung lebih cepat muncul; perlu verifikasi ulang saat implementasi T-017).
3. **On-chain OrderFilled (backlog v1) tetap jalur paling lengkap** — memuat maker/taker wallet secara langsung, satu-satunya jalur satu-hop untuk alert per wallet ≤ 10 s.
4. WS hanya untuk **harga real-time** di MVP (mis. `price` sekarang di alert T-017) — bukan sumber trade.

## Batasan

- Latency diukur dari 3 run × 5,5 mnt (2 market per run) pada market ramai; pasar sepi/di luar jam ramai bisa berbeda, tapi angka 80–90 ms jauh dari batas 10 s sehingga margin aman besar.
- Delay "trade terjadi → tersedia di Data API" untuk trade yang di-trigger WS tidak diukur terpisah (hingga 400 event pertama disimpan; perbandingan wallet↔tx dilakukan pasca-run). Ini satu-satunya celah untuk target end-to-end ≤ 10 s dan harus diverifikasi saat T-017.
- Batas token per koneksi tidak diuji (hanya 4 token); volume `price_change` tinggi bisa jadi pertimbangan bandwidth.
- Tidak diuji: reconnect otomatis dengan backoff, multi-koneksi paralel, `user` channel (butuh API key — di luar scope, dan key milik user bukan milik sistem).
