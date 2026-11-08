# MVP — polywatch

> Status: Draft v0.1 · Target rilis: ±3 minggu setelah planning disetujui

---

## 1. Definisi MVP

Bot Telegram satu-owner yang otomatis mencatat hasil semua market crypto Up/Down 5m & 15m, merekam trade per wallet, memberi skor wallet berbasis edge, dan mengirim alert copy-trade saat wallet berskor tinggi membuka posisi. Belum ada eksekusi order.

## 2. Core Flow (Satu Alur Utama)

```
Market baru terdeteksi → trade wallet di-ingest → wallet berskor tinggi BUY → alert Telegram ke owner
→ market resolve → hasil (open/close/result/volume) tercatat → skor wallet diperbarui
```

Jika alur ini berjalan mulus dari awal sampai akhir di produksi, MVP dianggap berhasil.

## 3. Fitur Must-Have & Kriteria Selesai

### F1 — Discovery market & pengaturan market dipantau
- Deteksi otomatis market `{aset}-updown-{5m|15m}-*` dari Gamma API; aset baru terdeteksi tanpa ubah kode.
- Owner memilih aset × timeframe yang dipantau (`/markets`).
- **Selesai jika:**
  - [ ] Market berikutnya untuk tiap aset × timeframe aktif tersimpan sebelum market dimulai
  - [ ] Mematikan "DOGE 5m" lewat bot menghentikan ingest market itu mulai siklus berikutnya

### F2 — Arsip hasil market
- Simpan per market: aset, timeframe, waktu mulai/selesai, open price, close price, result (Up/Down), volume, sumber harga.
- **Selesai jika:**
  - [ ] ≥ 99% market yang dipantau punya baris hasil lengkap ≤ 2 menit setelah resolve (cek 24 jam)
  - [ ] Result yang tersimpan sama dengan resolusi resmi Polymarket (uji 50 sampel)
  - [ ] `/results btc 15m` menampilkan 10 hasil terakhir

### F3 — Ingest trade per wallet
- Rekam setiap trade: wallet (`proxyWallet`), market, sisi (Up/Down), buy/sell, harga, size, timestamp, tx hash. Idempoten (tanpa duplikat).
- **Selesai jika:**
  - [ ] Untuk 20 market acak, jumlah trade di DB = jumlah di Data API (toleransi ≤ 2%)
  - [ ] Menjalankan ulang ingest market yang sama tidak membuat duplikat

### F4 — Skor wallet
- Per wallet, dari posisi bersih per market yang sudah resolve: jumlah market, win rate, rata-rata harga masuk, **edge** (win rate − harga masuk rata-rata), PnL, ROI, dan skor 0–100 dengan penyusutan untuk sampel kecil.
- Filter: market maker / pegang dua sisi, sampel < minimum (default 50 market).
- Dihitung ulang setiap ada market resolve.
- **Selesai jika:**
  - [ ] Unit test: wallet beli di 0.90 menang 85% → edge negatif, skor rendah
  - [ ] Unit test: wallet yang pegang Up & Down di market sama ditandai market maker
  - [ ] `/top` menampilkan 10 wallet teratas; `/wallet <alamat>` menampilkan statistik lengkap

### F5 — Alert copy-trade
- Saat wallet dengan skor ≥ ambang (default 70) atau ada di watchlist membuka posisi ≥ ukuran minimal → kirim alert: wallet, skor, aksi, sisi, harga, size, market, sisa waktu, harga sekarang, link market.
- Pengaturan: skor minimal, size minimal, watchlist (`/watch`, `/unwatch`), jeda anti-spam per wallet/market.
- **Selesai jika:**
  - [ ] Trade simulasi dari wallet skor tinggi menghasilkan tepat satu alert
  - [ ] Median delay trade → alert ≤ 10 detik selama 24 jam (dicatat di log)
  - [ ] Alert tidak terkirim untuk wallet di bawah ambang / di luar market yang dipantau

### F6 — Operasional
- Health check, log terstruktur, notifikasi error ke owner, backup harian PostgreSQL.
- **Selesai jika:**
  - [ ] `/status` menampilkan: market aktif, delay ingest terakhir, error terakhir
  - [ ] Backup harian berjalan dan restore teruji sekali

## 4. Tidak Masuk MVP (Ditunda)

| Ide | Alasan ditunda | Target |
|---|---|---|
| Paper copy-trade (simulasi dengan delay & harga nyata) | Butuh data MVP dulu | v1 |
| Ingest via event on-chain Polygon | Hanya jika polling tidak memenuhi latency | v1 |
| Multi-user (tiap orang punya pengaturan sendiri) | Fokus owner dulu | v2 |
| **Wallet EVM otomatis per pengguna** (dibuat saat `/start`) | Menyangkut kunci & dana, butuh bukti edge v1 | v2 |
| **Eksekusi copy-trade otomatis** + alert | Sama | v2 |
| Batas risiko, kill switch, deposit/withdraw USDC | Bagian dari eksekusi | v2 |
| Dashboard web, backtest interaktif | Bukan inti | v3 |

## 5. Definition of Done — MVP

- [ ] Semua fitur F1–F6 memenuhi kriteria selesainya.
- [ ] Test unit, integration, dan e2e core flow lulus.
- [ ] Aplikasi berjalan di VPS dan health check hijau.
- [ ] Backup database aktif dan sudah diuji restore.
- [ ] `README.md`, `CHANGELOG.md`, dan keenam dokumen di `Detail Project/` terbarui.
- [ ] Tag rilis `v0.1.0` dibuat di GitHub.
- [ ] Demo berhasil dijalankan bersama owner.

## 6. Skenario Demo (Acceptance)

1. `/markets` → aktifkan BTC & ETH 5m/15m. `/status` menunjukkan 4 market aktif.
2. Tunggu 1 siklus 15 menit → `/results btc 15m` menampilkan open, close, result, volume yang sama dengan Polymarket.
3. `/top` → pilih satu wallet → `/watch <alamat>`. Saat wallet itu trade, alert masuk ≤ 10 detik berisi harga sekarang dan link market.
