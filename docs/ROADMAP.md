# ROADMAP — polywatch

> Status: Draft v0.1 · Diperbarui: 2026-11-08
> Estimasi kasar; dievaluasi ulang di akhir tiap milestone. Minggu dihitung sejak planning disetujui.

---

## Ringkasan Timeline

| Milestone | Fokus | Periode | Rilis |
|---|---|---|---|
| **M0** | Fondasi, tooling, spike sumber data | Minggu 1 (awal) | — |
| **M1** | Discovery market + arsip hasil (F1, F2) | Minggu 1 | — |
| **M2** | Ingest trade + skor wallet (F3, F4) | Minggu 2 | — |
| **M3** | Bot Telegram + alert copy-trade (F5) | Minggu 2–3 | — |
| **M4** | Operasional, deploy & demo MVP (F6) | Minggu 3 | **v0.1.0 (MVP)** |
| **v1** | Paper copy-trade + optimasi latency | +2–3 minggu | v0.2.0 |
| **v2** | Wallet EVM per pengguna + eksekusi copy-trade | +3–4 minggu | v1.0.0 |

---

## M0 — Fondasi & Tooling

**Deliverable**
- Repo GitHub `ryzzu0x/polywatch`, struktur Clean Architecture, import-linter
- ruff, mypy, pytest, CI GitHub Actions
- PostgreSQL lokal + Alembic
- Spike: verifikasi endpoint Data API (pagination, rate limit, latency) dan sumber open/close price

**Kriteria keluar:** CI hijau; laporan spike tercatat di DECISIONS (ADR-004, ADR-005).

---

## M1 — Discovery Market & Arsip Hasil

**Deliverable**
- Use case `discover_markets`, `record_result`; tabel `markets`, `market_results`, `watch_config`

**Kriteria keluar:** F1 dan F2 di MVP.md terpenuhi di lokal selama 24 jam.

**Risiko utama:** sumber open/close tidak sama persis dengan Chainlink → simpan `price_source`, bandingkan result dengan resolusi resmi.

---

## M2 — Ingest Trade & Skor Wallet

**Deliverable**
- `ingest_trades` (idempoten), `positions`, `recompute_scores`, filter market maker

**Kriteria keluar:** F3 dan F4 terpenuhi; unit test skor lulus.

**Risiko utama:** rate limit saat 14 market live paralel → batching & backoff.

---

## M3 — Bot Telegram & Alert

**Deliverable**
- Semua perintah MVP, `evaluate_alert`, formatter alert, pengaturan alert & watchlist

**Kriteria keluar:** F5 terpenuhi; median delay ≤ 10 detik di lokal.

---

## M4 — Deploy & Demo MVP

**Deliverable**
- `scripts/deploy.sh`, `scripts/backup.sh`, unit systemd `polywatch.service`
- Deploy ke VPS + verifikasi (health, log, satu siklus market penuh)
- README, CHANGELOG, tag `v0.1.0`, demo dengan owner

**Kriteria keluar:** seluruh Definition of Done MVP di `MVP.md` tercentang.

---

## Pasca-MVP (Tentatif)

### v1 — Paper copy-trade
- Simulasi copy setiap alert: harga masuk = harga CLOB saat alert + delay nyata, size tetap.
- Laporan harian & mingguan: ROI paper per wallet dan total.
- Bila latency polling tidak cukup → ingest via event on-chain Polygon (OrderFilled).
- **Gate ke v2:** ROI paper positif selama ≥ 2 minggu.

### v2 — Wallet per pengguna + eksekusi
- Bot multi-user: setiap orang yang menjalankan `/start` otomatis mendapat **wallet EVM baru** (eth-account), private key dienkripsi.
- Pengguna deposit USDC (Polygon) ke alamatnya; bot menyiapkan approval & kredensial CLOB dari wallet itu.
- **Copy-trade otomatis**: saat alert terpicu, bot langsung memasang order sesuai aturan pengguna (wallet yang di-copy, ukuran tetap/persentase), **dan** tetap mengirim alert berisi hasil eksekusi.
- Pengaman: batas per trade, batas harian, stop-loss harian, kill switch `/stop`, mode paper default untuk pengguna baru.
- Withdraw dan ekspor private key oleh pengguna.

### v3
- Dashboard web, backtest strategi, sumber data tambahan.

---

## Aturan Perubahan Roadmap

1. Fitur baru masuk backlog dulu, bukan langsung ke milestone berjalan.
2. Perubahan scope MVP hanya lewat persetujuan owner dan dicatat di `DECISIONS.md`.
3. Bila milestone terlambat lebih dari 2 hari, agent mengusulkan pemotongan scope, bukan pengunduran tanpa batas.
