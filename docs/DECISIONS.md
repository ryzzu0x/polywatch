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
| ADR-004 | Sumber trade: polling Data API (MVP) | Diusulkan — diverifikasi spike T-005 |
| ADR-005 | Sumber open/close price | Terbuka — diputuskan di spike T-005 |
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

### ADR-004 — Sumber trade: polling Data API (MVP)
- **Tanggal:** 2026-11-08
- **Status:** Diusulkan — diverifikasi spike T-005
- **Konteks:** Perlu trade per wallet dengan delay rendah. Pengecekan awal: Data API `/trades?market=<conditionId>` mengembalikan trade dengan alamat wallet.
- **Keputusan:** Polling per market live tiap 2–3 detik dengan cursor waktu, insert idempoten.
- **Alternatif:** Event on-chain Polygon (OrderFilled) via RPC websocket — paling cepat & lengkap tetapi lebih kompleks (decode log, pemetaan token → market, biaya RPC). Dipertimbangkan di v1 bila target delay ≤ 10 detik tidak tercapai.
- **Konsekuensi:** (+) Cepat dibangun. (−) Tergantung rate limit & latency API pihak ketiga (TD-001).

### ADR-005 — Sumber open/close price
- **Tanggal:** 2026-11-08
- **Status:** Terbuka
- **Konteks:** Market resolve pakai Chainlink (TWAP rentang waktu vs harga di awal rentang). Open/close yang dicatat harus sejalan dengan resolusi.
- **Keputusan:** Diputuskan setelah spike T-005 membandingkan kandidat sumber terhadap 50 resolusi resmi. Kolom `price_source` selalu disimpan.
- **Alternatif:** Chainlink langsung; API harga publik (mis. Binance) sebagai cadangan.
- **Konsekuensi:** Result resmi selalu diambil dari Polymarket; harga hanya sebagai informasi.

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
