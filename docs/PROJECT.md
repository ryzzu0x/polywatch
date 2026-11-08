# PROJECT — polywatch

> Status: Draft v0.1 · Tanggal: 2026-11-08 · Owner: ryzzu

---

## 1. Ringkasan

polywatch adalah bot Telegram yang memantau market crypto "Up or Down" 5 menit dan 15 menit di Polymarket. Bot mencatat hasil setiap market (open, close, result, volume), merekam semua trade per wallet, memberi skor wallet berdasarkan edge nyata, lalu mengirim **alert copy-trade** saat wallet berskor tinggi membuka posisi. Di v2, setiap pengguna bot mendapat wallet EVM sendiri yang dibuat otomatis, sehingga copy-trade bisa dieksekusi langsung dari bot di samping alert.

## 2. Masalah yang Diselesaikan

- Tidak ada catatan historis rapi untuk hasil market 5m/15m (open, close, result, volume) per aset.
- Sulit membedakan wallet yang benar-benar punya edge dari wallet yang beruntung, market maker, atau bot dua sisi.
- Market 5m/15m bergerak cepat. Mengikuti wallet bagus secara manual terlambat; butuh sinyal otomatis dan nantinya eksekusi otomatis.

## 3. Tujuan (Terukur)

| # | Tujuan | Ukuran keberhasilan |
|---|---|---|
| G1 | Arsip hasil market lengkap | ≥ 99% market 5m/15m aset aktif tercatat dengan open, close, result, volume dalam ≤ 2 menit setelah resolve |
| G2 | Rekam trade per wallet | ≥ 98% trade di market yang dipantau masuk DB (dicek acak terhadap Data API) |
| G3 | Skor wallet yang jujur | Skor memakai edge (win rate − rata-rata harga masuk), ROI, dan sampel minimum; wallet market maker terfilter |
| G4 | Alert cepat | Median delay trade wallet → alert Telegram ≤ 10 detik |
| G5 (v1) | Bukti edge bertahan | Paper copy-trade 2 minggu dengan delay nyata menunjukkan ROI positif sebelum v2 diaktifkan untuk uang sungguhan |

## 4. Target Pengguna

**Persona utama — ryzzu (owner, trader Polymarket)**
Aktif trading di market crypto pendek, memakai Telegram di HP, ingin tahu wallet mana yang layak diikuti dan langsung mendapat sinyal.

**Persona sekunder (v2) — pengguna publik bot**
Siapa pun yang menjalankan `/start` di bot. Mendapat wallet EVM baru, mengisi USDC, memilih wallet/market yang di-copy, dan membiarkan bot mengeksekusi dengan batas risiko.

## 5. Ruang Lingkup

### In scope (MVP)
- Penemuan otomatis semua market crypto Up/Down 5m & 15m (saat ini: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE), dengan aset baru terdeteksi otomatis.
- Pengaturan market yang dipantau (aset × timeframe), bisa diubah lewat bot.
- Pencatatan hasil market: open price, close price, result (Up/Down), volume.
- Ingest semua trade (buy/sell) per wallet di market yang dipantau.
- Skor wallet berbasis edge + filter market maker + sampel minimum.
- Alert Telegram saat wallet berskor tinggi membuka posisi.
- Perintah bot: lihat hasil market, top wallet, detail wallet, watchlist wallet, pengaturan alert.

### Non-goals (sengaja tidak dikerjakan sekarang)
- Eksekusi order / copy-trade otomatis (v2).
- Wallet EVM per pengguna (v2).
- Multi-user publik (MVP hanya owner; skema DB disiapkan `user_id` agar v2 tidak perlu migrasi besar).
- Market Polymarket selain crypto Up/Down 5m/15m.
- Dashboard web.

## 6. Asumsi

1. Data publik Polymarket cukup: Gamma API (market), Data API (trade per wallet, `proxyWallet`), CLOB (harga). Sudah dicek 2026-11-08: slug `{aset}-updown-{5m|15m}-{unix_start}` aktif untuk 7 aset.
2. Market resolve berdasarkan Chainlink (TWAP rentang waktu vs harga awal). Open/close price diambil dari sumber yang sama atau yang paling mendekati; sumber pasti ditentukan di spike T-005.
3. Polling Data API per beberapa detik cukup untuk MVP; bila latency tidak tercapai, beralih ke event on-chain Polygon (v1).
4. MVP dipakai satu owner (ryzzu); alert dikirim ke chat owner/topic yang dikonfigurasi.
5. VPS 2 vCPU / 1.9 GiB cukup: ±2.700 market/hari (7 aset × 384).

## 7. Batasan

- **Waktu:** MVP target ±3 minggu (lihat ROADMAP).
- **Tim:** ryzzu (owner) + agent.
- **Infrastruktur:** VPS Ubuntu 24.04 milik owner (2 vCPU, 1.9 GiB, Python 3.12, tanpa Docker), systemd, PostgreSQL.
- **Stack:** Python 3.12 + asyncio, aiogram 3, PostgreSQL 16, SQLAlchemy 2 + Alembic, httpx. (Alasan di DECISIONS ADR-002.)

## 8. Risiko & Mitigasi

| Risiko | Dampak | Kemungkinan | Mitigasi |
|---|---|---|---|
| Win rate menyesatkan (beli mahal, menang sering, tetap rugi) | tinggi | tinggi | Skor pakai edge + ROI dari posisi bersih per market, bukan per transaksi |
| Wallet market maker / bot dua sisi tampak bagus | tinggi | tinggi | Filter: pegang dua sisi di market sama, rasio maker tinggi, jumlah trade ekstrem |
| Sampel kecil = beruntung | sedang | tinggi | Sampel minimum (default 50 market) + skor dengan penyusutan (Bayesian shrinkage) |
| Latency membuat edge hilang | tinggi | sedang | Ukur delay nyata; paper trading v1 sebelum uang sungguhan |
| Rate limit / perubahan API Polymarket | sedang | sedang | Backoff, cache, adapter terisolasi di infrastructure, alert error ke owner |
| (v2) Kebocoran private key pengguna | kritis | rendah–sedang | Enkripsi at-rest (master key di env, bukan DB), tanpa log key, ekspor key hanya atas permintaan pengguna |
| (v2) Kerugian dana pengguna karena bug eksekusi | kritis | sedang | Batas modal per trade/harian, kill switch, paper mode default, audit log |
| (v2) Geoblock & aspek hukum bot kustodian publik | tinggi | sedang | Dibahas dan diputuskan owner sebelum v2 (ADR terpisah) |

## 9. Metrik Keberhasilan MVP

- Bot berjalan 7 hari berturut-turut di VPS tanpa restart manual.
- Arsip hasil market memenuhi G1; ingest trade memenuhi G2.
- Owner menerima alert dengan median delay ≤ 10 detik dan menilai ≥ 1 wallet layak dipantau.

## 10. Dokumen Terkait

- [MVP.md](./MVP.md) · [ARCHITECTURE.md](./ARCHITECTURE.md) · [ROADMAP.md](./ROADMAP.md) · [TASKS.md](./TASKS.md) · [DECISIONS.md](./DECISIONS.md)
