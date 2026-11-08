# DECISIONS — polywatch

> Catatan keputusan teknis penting (Architecture Decision Record, ringkas).
> Tambahkan entri baru di bagian bawah. Jangan menghapus keputusan lama; ubah statusnya menjadi *Digantikan* dan tautkan ke keputusan baru.

---

## Template

```
### ADR-XXX — Judul keputusan
- Tanggal: YYYY-MM-DD
- Status: Diusulkan | Diterima | Diterima sementara | Digantikan oleh ADR-YYY
- Konteks: masalah atau kebutuhan yang mendorong keputusan
- Keputusan: apa yang dipilih
- Alternatif: opsi lain yang dipertimbangkan dan alasan tidak dipilih
- Konsekuensi: dampak positif, negatif, dan hal yang harus dijaga
```

## Ringkasan

| ID | Keputusan | Status |
|---|---|---|
| ADR-001 | Clean Architecture dengan aturan boundary di lint | Diterima |
| ADR-002 | Python 3.12 + aiogram + PostgreSQL, monolith satu proses | Diterima |
| ADR-003 | Skor wallet berbasis edge & ROI, bukan win rate | Diusulkan |
| ADR-004 | Sumber trade MVP: hybrid WS CLOB + RPC Polygon (OrderFilled), backfill Data API | Diterima (spike T-005, T-005b, T-005c; T-011 terimplementasi, 2026-11-08) |
| ADR-005 | Sumber open/close price: Polymarket crypto-price (Chainlink), cadangan Binance | Diterima (spike T-005, 2026-11-08) |
| ADR-006 | MVP alert saja; wallet & eksekusi di v2 dengan gate paper trading | Diterima |
| ADR-007 | v2: wallet EVM per pengguna, kunci dienkripsi di server (kustodian) + ekspor oleh pengguna | Diterima |
| ADR-008 | State health monitor in-memory (bukan tabel baru), notifikasi operasional via port Notifier | Diterima (T-019, 2026-11-08) |

---

### ADR-001 — Clean Architecture dengan aturan boundary di lint
- **Tanggal:** 2026-11-08
- **Status:** Diterima
- **Konteks:** Project akan berkembang dari MVP ke versi berikutnya. Logika bisnis harus mudah dites dan tidak terikat framework.
- **Keputusan:** Pakai layer `domain → application → infrastructure/presentation` dengan port/adapter. Aturan ketergantungan dipaksa oleh import-linter.
- **Alternatif:** Script sederhana (cepat di awal tetapi logika skor & eksekusi v2 akan sulit dites); hexagonal penuh dengan banyak abstraksi (terlalu berat untuk MVP).
- **Konsekuensi:** (+) Skor & aturan copy bisa dites tanpa DB/HTTP; ganti sumber data murah. (−) Sedikit lebih banyak file di awal.

### ADR-002 — Python 3.12 + aiogram + PostgreSQL, monolith satu proses
- **Tanggal:** 2026-11-08
- **Status:** Diterima (disetujui owner di chat 2026-11-08)
- **Konteks:** Butuh I/O async ke beberapa API, bot Telegram, dan nanti (v2) penandatanganan order Polymarket & pembuatan wallet EVM.
- **Keputusan:** Python 3.12 asyncio, aiogram 3, httpx, SQLAlchemy 2 async + Alembic, PostgreSQL 16, satu service systemd.
- **Alternatif:** Go (dipakai di tekun, lebih hemat RAM, tetapi SDK resmi Polymarket `py-clob-client` dan tooling EVM lebih matang di Python); SQLite (lebih ringan, tetapi tulis paralel & query analitik skor lebih lemah).
- **Konsekuensi:** (+) Jalur v2 paling pendek (py-clob-client, eth-account), Python sudah ada di VPS. (−) Konsumsi RAM lebih besar dari Go; perlu disiplin type checking (mypy).

### ADR-003 — Skor wallet berbasis edge & ROI, bukan win rate
- **Tanggal:** 2026-11-08
- **Status:** Diusulkan
- **Konteks:** Di market biner, wallet yang selalu beli di harga 0.90 bisa menang 85% tetapi rugi. Win rate saja menyesatkan.
- **Keputusan:** Hitung dari posisi bersih per market yang sudah resolve: edge = win rate − rata-rata harga masuk (berbobot USDC), ROI, PnL. Skor 0–100 = kombinasi edge & ROI dengan penyusutan untuk sampel kecil (default minimum 50 market). Wallet yang memegang dua sisi di market sama secara rutin ditandai market maker dan dikeluarkan dari alert.
- **Alternatif:** Win rate murni (menyesatkan); PnL absolut (bias ke wallet bermodal besar).
- **Konsekuensi:** (+) Sinyal lebih jujur. (−) Wallet baru butuh waktu sebelum bisa dinilai; parameter perlu dikalibrasi dengan data nyata di v1.

### ADR-004 — Sumber trade: hybrid WS CLOB + RPC Polygon (OrderFilled) dengan backfill Data API
- **Tanggal:** 2026-11-08
- **Status:** Diterima (diverifikasi spike T-005 dan spike T-005b dengan angka nyata; laporan: `docs/spikes/T-005-sumber-data.md`, `docs/spikes/T-005b-websocket.md`)
- **Konteks:** Perlu trade per wallet dengan delay rendah. Pengecekan awal: Data API `/trades?market=<conditionId>` mengembalikan trade dengan alamat wallet.
- **Temuan spike (2026-11-08):**
  - Data API **v1 sudah pensiun (HTTP 410)**; wajib `https://data-api.polymarket.com/v2/trades`.
  - Filter per market: `?condition=<conditionId>` (bukan `market=`; `token_id` diterima tapi diabaikan — terbukti mengembalikan firehose global).
  - Pagination **cursor-only** (`next_cursor`); `offset` ditolak 400; `limit` per halaman max 500 (terbukti).
  - Field per trade lengkap: `proxy_wallet`, `side`, `token_id`, `condition_id`, `size`, `price`, `timestamp`, `outcome`, `outcome_index`, `transaction_hash`.
  - Duplikat: polling berulang mengembalikan baris sama (1.400/1.500 baris duplikat) → insert wajib idempoten.
  - **Latency trade→tersedia: median 164 s, p90 251 s** (82 trade baru, market live sol-5m, poll tiap 4 s) — jauh dari target ≤ 10 s.
  - Rate limit: 10 request berurutan tanpa jeda semua 200; tidak ada header `x-ratelimit-*`; tidak diuji sampai kena limit (sengaja).
- **Keputusan:** Polling `GET /v2/trades?condition=<conditionId>&limit=500` dengan cursor, insert idempoten (`transaction_hash`+`token_id`+`price`+`size`+`timestamp`), jeda antar poll 2–3 s, total ≤ 5 req/s.
- **Alternatif:** Event on-chain Polygon (OrderFilled) via RPC websocket — paling cepat & lengkap tetapi lebih kompleks (decode log, pemetaan token → market, biaya RPC). Karena latency polling terukur menit-bukan-detik, event on-chain naik prioritas untuk v1.
- **Konsekuensi:** (+) Cepat dibangun, field cukup untuk skor wallet. (−) Delay data trade median ~2,7 menit → analisis delay (TD-001) harus pakai `timestamp` trade, bukan waktu insert.
- **Temuan tambahan spike T-005b (CLOB WebSocket market channel, 2026-11-08):**
  - `wss://ws-subscriptions-clob.polymarket.com/ws/market` + subscribe `{"assets_ids": [...], "type": "market"}` bekerja publik tanpa API key; server minta heartbeat text-frame `"PING"`/`"PONG"`.
  - Event: `book`, `price_change`, `last_trade_price`, `tick_size_change`. `last_trade_price` memuat `transaction_hash`, `price`, `size`, `side`, `timestamp`, `fee_rate_bps`, `market` — **TIDAK memuat alamat wallet** (tidak ada `proxy_wallet`/maker/taker).
  - **Latency event→terima: median ~81 ms, p90 ~85 ms, max 592 ms** (49.422 event, 3 run × 5,5 mnt, market 5m ramai) — jauh di bawah target ≤ 10 s.
  - Trade WS ↔ Data API cocok satu-satu via `transaction_hash`; wallet hanya tersedia di Data API.
  - Server menutup koneksi (close 1000, `"all subscribed assets resolved"`) saat semua market resolve → **reconnect + re-subscribe wajib** tiap siklus; batas token per koneksi tidak terukur (diuji 4 token).
- **Konsekuensi tambahan:** WS market channel hanya untuk **harga real-time** (mis. harga sekarang di alert T-017) dan di v1 sebagai *detector* (`transaction_hash`) yang memicu poll Data API untuk mengambil `proxy_wallet` — hybrid WS+poll. Ingest on-chain OrderFilled (backlog v1) tetap satu-satunya jalur satu-hop untuk alert per wallet ≤ 10 s.

- **Temuan tambahan spike T-005c (OrderFilled via RPC Polygon, 2026-11-08; laporan `docs/spikes/T-005c-orderfilled-rpc.md`):**
  - Seluruh market Up/Down live tereksekusi di **CTF Exchange V2** `0xE111180000d2663C0091e4f400237545B87B996B` (Neg Risk Exchange & exchange V1 tidak muncul di 39 tx yang diamati).
  - Event kunci: `OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)` — topic0 `0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee`; data 7 word statis, decodable tanpa library ABI.
  - Memuat **maker/taker wallet** + tokenId + jumlah + arah — semua yang dibutuhkan `Trade`; validasi silang 39/39 tx: tokenId/size/price cocok event WS.
  - **Latency end-to-end (WS diterima → ter-decode/tersimpan): median 855 ms, p90 1.683 ms, max 2.128 ms** (39 trade, 4 market 5m) — ~190× lebih cepat dari Data API (median 164 s) dan jauh di bawah target ≤ 10 s.
  - **Event WS datang PRA-KONFIRMASI**: receipt `null` pada 7–9 ms pertama (tx belum masuk blok) → wajib poll receipt (~250 ms) sampai hadir; waktu dominan e2e = menunggu block Polygon (~2 s). `eth_getTransactionReceipt` cukup — tidak perlu `eth_getLogs`.
  - Latensi RPC per endpoint (15 panggilan): Alchemy 7,5 ms; drpc 15,9 ms; publicnode 186 ms; 1rpc 269 ms. Failover urutan: Alchemy (bila ada) → publicnode → 1rpc → drpc.
- **Revisi keputusan (T-011, 2026-11-08):** Ingest trade MVP = **hybrid WS + RPC**: WS `last_trade_price` sebagai detector tx (dan harga real-time) → `eth_getTransactionReceipt` (failover) → decode `OrderFilled` → insert idempoten `(tx_hash, log_index, wallet)`. Data API v2 tetap sebagai **backfill** gap (WS putus / market resolve sebelum sempat) — bukan jalur utama. Tidak lagi "polling Data API utama, hybrid di v1".
- **Konsekuensi revisi:** (+) Alert per wallet ≤ 10 s tercapai di MVP (e2e < 2,2 s terukur); tidak menunggu delay Data API 164 s. (−) Perlu kelola koneksi WS (reconnect tiap siklus market 5m/15m) + failover RPC; ketergantungan kontrak exchange V2 (alamat/event di-hardcode — perlu tabel kontrak bila scope meluas ke market neg-risk).

### ADR-005 — Sumber open/close price: Polymarket crypto-price (Chainlink selaras), cadangan Binance
- **Tanggal:** 2026-11-08
- **Status:** Diterima (diputuskan spike T-005; laporan: `docs/spikes/T-005-sumber-data.md`)
- **Konteks:** Market resolve pakai Chainlink TWAP 60 s (`resolutionSource` market: `data.chain.link/streams/btc-usd-twap-60s-streams`). Open/close yang dicatat harus sejalan dengan resolusi.
- **Temuan spike (2026-11-08, 48 market resolved dibandingkan):**
  - **Utama — `GET polymarket.com/api/crypto/crypto-price?symbol=<aset>&eventStartTime=<unix>`**: mengembalikan `openPrice`/`closePrice` dari feed Chainlink yang sama dengan resolusi. Diverifikasi pada 4 window live: `openPrice` konstan selama window aktif, **3/4 window cocok dengan resolusi resmi**; 1 mismatch karena `openPrice` stale saat pergantian window (konstan 75918.01 selama 3 window sampel). Batasan: `eventStartTime` historis diabaikan → open dicatat saat window mulai, close saat window berakhir (tidak bisa backfill).
  - **Cadangan — Binance klines 1m**: akurasi **41/48 (85,4%)** terhadap resolusi resmi (spot dan TWAP menit sama-sama 41/48); median selisih pada 7 mismatch hanya US$0,03 — mismatch karena TWAP Chainlink ≠ spot Binance, bukan gap besar. Dipakai sebagai fallback tampilan/volatilitas, bukan acuan arah resolusi.
  - Chainlink Data Streams langsung: **tidak terukur** (butuh API key; web UI kena bot-checkpoint).
  - Resolusi resmi sendiri: Gamma `?closed=true` + `umaResolutionStatus="resolved"` + `outcomePrices` (delay resolve setelah end terukur 343 s pada sampel; poll dengan toleransi ±6 menit).
- **Keputusan:** Sumber open/close utama = Polymarket `crypto-price` (`price_source="polymarket-chainlink"`); cadangan = Binance 1m klines (`price_source="binance-1m"`); kolom `price_source` selalu disimpan. Resolusi resmi selalu diambil dari Gamma, harga hanya informasi.
- **Alternatif:** Chainlink langsung (butuh kredensial, tidak terukur); Binance sebagai utama (akurasi 85% < kandidat selaras resolusi).
- **Konsekuensi:** (+) Selaras resolusi, tanpa API key. (−) Tidak bisa backfill historis lewat crypto-price → producer harus online saat window berjalan; `openPrice` pernah stale saat pergantian window → catat kedua sumber (`price_source`) dan audit akurasi otomatis saat producer berjalan; evaluasi ulang di v1 bila akurasi crypto-price < Binance (85,4%).

### ADR-006 — MVP alert saja; wallet & eksekusi di v2 dengan gate paper trading
- **Tanggal:** 2026-11-08
- **Status:** Diterima (disetujui owner di chat 2026-11-08)
- **Konteks:** Eksekusi otomatis menyangkut private key & dana. Edge wallet belum terbukti bertahan setelah delay.
- **Keputusan:** MVP hanya alert. v1 paper copy-trade. v2 mengaktifkan wallet & eksekusi setelah paper ROI positif ≥ 2 minggu.
- **Alternatif:** Langsung eksekusi di MVP (risiko kehilangan dana tanpa bukti edge).
- **Konsekuensi:** (+) Risiko dana nol di MVP. (−) Nilai eksekusi baru terasa di v2.

### ADR-007 — v2: wallet EVM per pengguna, kunci dienkripsi di server (kustodian) + ekspor
- **Tanggal:** 2026-11-08
- **Status:** Diterima (disetujui owner di chat 2026-11-08), dengan syarat: pengguna dapat mengekspor kunci sendiri
- **Konteks:** Owner ingin setiap orang yang menjalankan bot otomatis mendapat wallet EVM baru dan copy-trade dieksekusi langsung, di samping alert.
- **Keputusan:** Saat `/start` (v2), bot membuat keypair dengan `eth-account`, menyimpan private key terenkripsi AES-GCM (master key dari env `WALLET_MASTER_KEY`, tidak pernah di DB/log). Pengguna dapat mengekspor private key miliknya kapan pun (`/export_key`: konfirmasi eksplisit, pesan berisi kunci dihapus otomatis) sehingga dana tidak terkunci di server. Kredensial CLOB diturunkan dari wallet tersebut. Eksekusi mengikuti `copy_rules` + batas risiko pengguna; setiap order dan hasilnya dikirim sebagai alert.
- **Alternatif:** Pengguna menghubungkan wallet sendiri (non-kustodian, lebih aman tetapi UX berat untuk Telegram); satu wallet bersama (tidak bisa memisahkan dana pengguna).
- **Konsekuensi:** (+) UX paling mudah: `/start` → deposit → copy. (−) Server menjadi kustodian dana pengguna: risiko keamanan tinggi, perlu backup master key, dan aspek hukum/geoblock Polymarket harus diputuskan owner sebelum rilis publik.

### ADR-008 — State health monitor in-memory, notifikasi operasional lewat port Notifier
- **Tanggal:** 2026-11-08
- **Status:** Diterima (T-019)
- **Konteks:** Owner butuh tahu saat ingest dimatikan/mati > 2 menit atau siklus scheduler berhenti berhasil, tanpa spam notifikasi saat gangguan berjam-jam. Perlu juga notifikasi pemulihan. State kesehatan (aktivitas terakhir, status notifikasi, waktu kirim terakhir) harus disimpan di suatu tempat.
- **Keputusan:** Seluruh state kesehatan disimpan in-memory di objek `HealthMonitor` (use case) — tidak ada tabel/migrasi baru. Notifikasi keluar tetap lewat port `Notifier` (TelegramNotifier) supaya Clean Architecture terjaga. Rate limit 1 notifikasi per jenis per 10 menit (`HEALTH_RATE_LIMIT_S`), grace period 5 menit saat start tanpa data.
- **Alternatif:** Tabel `health_events` di DB (awet melewati restart, tetapi menambah migrasi + tulisan DB untuk data yang tidak dibutuhkan lintas restart di MVP); notifikasi langsung via aiogram dari use case (melanggar boundary).
- **Konsekuensi:** (+) Sederhana, nol biaya DB, mudah dites. (−) Restart proses mereset grace period dan status notifikasi: bila gangguan masih berlangsung, owner mungkin menerima notifikasi basi sekali lagi setelah restart — dapat diterima untuk MVP; kalau mengganggu, naikkan ke persistensi di v1.
