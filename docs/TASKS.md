# TASKS — polywatch

> Backlog kerja berurutan. Diperbarui oleh agent setiap task selesai.
> Estimasi: **S** ≤ 2 jam · **M** ≤ ½ hari · **L** ≤ 1 hari
> Status: `[ ]` belum · `[~]` berjalan · `[x]` selesai · `[!]` terblokir

---

## Aturan Task

- Kerjakan berurutan sesuai ID kecuali ada alasan jelas.
- Satu task = satu branch (`feature/T-011-nama-singkat`) = satu PR kecil.
- Task selesai hanya jika **Definition of Done** terpenuhi: kode berjalan, test baru + lama lulus, lint/build bersih, dokumen diperbarui bila perlu, commit jelas.
- Temuan atau ide baru di luar scope → tambahkan di **Backlog**, jangan langsung dikerjakan.

---

## M0 — Fondasi & Tooling

- [x] **T-001 · Inisialisasi repo & struktur Clean Architecture** — S
  - `pyproject.toml` (uv), struktur `src/polywatch/*`, ruff, mypy, pytest, import-linter, `.gitignore`, `.env.example`, README
  - *Selesai jika:* `ruff`, `mypy`, `lint-imports`, `pytest` hijau
- [x] **T-002 · CI GitHub Actions** — S
  - Workflow: install → ruff → mypy → import-linter → pytest (dengan service PostgreSQL)
  - *Selesai jika:* PR contoh hijau
- [ ] **T-003 · Config env, composition root, logging JSON** — S
  - Validasi env (pydantic-settings), container, entrypoint, heartbeat
  - *Selesai jika:* app start dengan env valid, gagal jelas dengan env tidak lengkap
- [ ] **T-004 · PostgreSQL lokal + Alembic + migrasi awal** — M
  - DB `polywatch_db`/`polywatch_test`, role `polywatch_app`, tabel sesuai ARCHITECTURE §4 (tanpa tabel v2)
  - *Selesai jika:* `alembic upgrade head` dan `downgrade base` berjalan bersih
- [ ] **T-005 · Spike sumber data** — M
  - Data API: pagination/cursor, rate limit, latency trade → tersedia; field `proxyWallet`, sisi, outcome
  - Open/close price: bandingkan Chainlink vs sumber publik lain terhadap 50 resolusi resmi
  - *Selesai jika:* ADR-004 & ADR-005 ditulis dengan angka nyata

## M1 — Discovery Market & Arsip Hasil (F1, F2)

- [ ] **T-006 · Domain Market/Timeframe/Outcome/MarketResult** — S
  - *Selesai jika:* unit test parsing slug & perhitungan waktu lulus
- [ ] **T-007 · Gamma client + `discover_markets`** — M
  - Auto-detect aset dari slug, upsert idempoten
  - *Selesai jika:* integration test (fixture) lulus; 24 jam lokal tanpa market terlewat
- [ ] **T-008 · `watch_config` + use case pengaturan market** — S
  - *Selesai jika:* menonaktifkan aset×tf menghentikan ingest siklus berikutnya (test)
- [ ] **T-009 · PriceSource + `record_result`** — M
  - Open/close, result, volume, retry sampai resolved, alert jika > 15 mnt
  - *Selesai jika:* 50 sampel result = resolusi resmi; ≥ 99% lengkap dalam 2 mnt

## M2 — Ingest Trade & Skor Wallet (F3, F4)

- [ ] **T-010 · Domain Trade & Position** — S
  - *Selesai jika:* unit test posisi bersih (buy/sell parsial, dua sisi) lulus
- [ ] **T-011 · Data API client + `ingest_trades` idempoten** — M
  - Cursor per market, backoff, batch insert
  - *Selesai jika:* 20 market acak cocok ±2% dengan Data API; ingest ulang tanpa duplikat
- [ ] **T-012 · Domain scoring (edge, ROI, shrinkage, filter MM)** — M
  - *Selesai jika:* unit test kasus "beli 0.90 menang 85% → edge negatif" & deteksi dua sisi lulus
- [ ] **T-013 · `recompute_scores` setelah resolve** — S
  - *Selesai jika:* wallet_stats terbarui ≤ 30 dtk setelah result tersimpan (integration test)

## M3 — Bot Telegram & Alert (F5)

- [ ] **T-014 · Bot dasar + guard owner** — S
  - aiogram, `/start`, `/status`; chat selain owner ditolak
  - *Selesai jika:* test handler lulus; manual cek di Telegram
- [ ] **T-015 · Perintah `/markets`, `/results`** — M
  - *Selesai jika:* skenario demo langkah 1–2 lulus
- [ ] **T-016 · Perintah `/top`, `/wallet`, `/watch`, `/unwatch`** — M
  - *Selesai jika:* validasi alamat & output sesuai spesifikasi (test)
- [ ] **T-017 · `evaluate_alert` + formatter + `/alerts`** — M
  - Ambang skor, size minimal, cooldown, harga sekarang, link market, catat `delay_ms`
  - *Selesai jika:* kriteria F5 lulus; median delay ≤ 10 dtk dalam 24 jam lokal
- [ ] **T-018 · E2E core flow** — M
  - market → trade → alert → resolve → skor dengan sumber palsu + DB nyata
  - *Selesai jika:* test e2e hijau di CI

## M4 — Operasional, Deploy & Demo (F6)

- [ ] **T-019 · Notifikasi error + heartbeat** — S
  - *Selesai jika:* ingest dimatikan > 2 mnt memicu notifikasi owner
- [ ] **T-020 · Script backup/restore** — S
  - *Selesai jika:* restore ke DB uji berhasil
- [ ] **T-021 · Deploy VPS (systemd, PostgreSQL, deploy.sh)** — M
  - Preflight port/unit/direktori, `.env` dibuat di VPS
  - *Selesai jika:* service jalan, `/status` hijau, satu siklus market penuh tercatat
- [ ] **T-022 · QA 7 hari + demo + tag v0.1.0** — S
  - *Selesai jika:* DoD MVP tercentang semua

---

## Backlog (Belum Dijadwalkan)

- [ ] v1: Paper copy-trade + laporan ROI harian/mingguan
- [ ] v1: Ingest via event on-chain Polygon bila latency polling kurang
- [ ] v2: Multi-user (users, pengaturan per pengguna)
- [ ] v2: Generate wallet EVM otomatis saat `/start` + enkripsi private key
- [ ] v2: Deposit/withdraw USDC, approval, kredensial CLOB per pengguna
- [ ] v2: Eksekusi copy-trade otomatis + alert hasil eksekusi
- [ ] v2: Batas per trade/harian, stop-loss harian, kill switch, mode paper default
- [ ] v2: Ekspor private key oleh pengguna (dengan konfirmasi, pesan auto-hapus)

## Technical Debt (Dicatat Sadar)

| ID | Deskripsi | Alasan ditunda | Dicatat |
|---|---|---|---|
| TD-001 | Ingest pakai polling Data API, bukan stream on-chain | Lebih cepat dibangun; dievaluasi di v1 | 2026-11-08 |

## Log Progres

| Tanggal | Task | Status | Catatan |
|---|---|---|---|
| 2026-11-08 | Planning | Draft | Dokumen planning v0.1 dibuat, menunggu review owner |
| 2026-11-08 | Planning | Disetujui | Owner menyetujui ADR-002 (Python) & ADR-007 (kustodian + ekspor key) |
| 2026-11-08 | T-001 | Selesai | Struktur Clean Architecture, config env tervalidasi, logging JSON, ruff/mypy strict/import-linter/pytest 27 test hijau |
| 2026-11-08 | T-002 | Selesai | Workflow CI `.github/workflows/ci.yml` (ruff, mypy, import-linter, pytest) |
