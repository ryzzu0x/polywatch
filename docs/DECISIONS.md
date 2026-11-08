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
| ADR-004 | Sumber trade: polling Data API v2 (MVP) | Diterima (spike T-005, 2026-11-08) |
| ADR-005 | Sumber open/close price: Polymarket crypto-price (Chainlink), cadangan Binance | Diterima (spike T-005, 2026-11-08) |
| ADR-006 | MVP alert saja; wallet & eksekusi di v2 dengan gate paper trading | Diterima |
| ADR-007 | v2: wallet EVM per pengguna, kunci dienkripsi di server (kustodian) + ekspor oleh pengguna | Diterima |

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

### ADR-004 — Sumber trade: polling Data API v2 (MVP)
- **Tanggal:** 2026-11-08
- **Status:** Diterima (diverifikasi spike T-005 dengan angka nyata; laporan: `docs/spikes/T-005-sumber-data.md`)
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
