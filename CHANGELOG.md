# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Added (T-005)
- Spike sumber data: laporan `docs/spikes/T-005-sumber-data.md` + contoh respons
  (`docs/spikes/t005-*.json`) + script probe ulang `scripts/spike/t005_probe.py`
  (throwaway).
- ADR-004 Diterima (Data API v2 `?condition=`, cursor-only, latency median 164 s /
  p90 251 s) dan ADR-005 Diterima (open/close: Polymarket `crypto-price` Chainlink
  utama, Binance 1m cadangan, akurasi 41/48). ARCHITECTURE §7 diperbarui.

### Added
- Fondasi proyek: struktur Clean Architecture (`domain`, `application`, `infrastructure`,
  `presentation`, `main`), konfigurasi env tervalidasi, logging JSON, entrypoint CLI.
- Domain awal: `MarketSlug`, `Timeframe`, `Asset`, `Outcome`, `WalletStats`.
- Port/interface: sumber market/trade/price, repository, notifier, clock.
- Tooling: ruff, mypy strict, import-linter (aturan layer), pytest, CI GitHub Actions.

### Added (T-003)
- Runner heartbeat (`main/runner.py`): loop log terstruktur tiap interval (default 60 dtk)
  berisi uptime & versi, lewat port `Clock`; shutdown rapi pada SIGINT/SIGTERM dan
  `stop_event` untuk test.
- Composition root lengkap (`main/container.py`): engine & session factory SQLAlchemy
  async dibuat lazy, `aclose()` untuk shutdown.
- CLI: default menjalankan runner; `--check` tetap validasi konfigurasi saja.
- Redaksi secret di `JsonFormatter`: extra field terindikasi secret
  (token/password/api_key/dsn/SecretStr) disamarkan.

### Added (T-004)
- PostgreSQL lokal: role `polywatch_app`, database `polywatch_db` & `polywatch_test`.
- Model SQLAlchemy 2 async (`infrastructure/db/models.py`): 11 tabel MVP
  (assets, watch_config, markets, market_results, trades, positions, wallet_stats,
  watchlist, alert_settings, alerts_sent, users) dengan `NUMERIC` untuk uang/harga
  serta index & unique constraint sesuai ARCHITECTURE §4. Tabel v2 ditunda.
- Alembic di `infrastructure/db/alembic` — `env.py` membaca `DATABASE_URL` dari
  `Settings` (tidak hardcode DSN). Migrasi awal `5dee4322c846`.
- Integration test DB (`tests/integration/test_db.py`) memakai `TEST_DATABASE_URL`,
  di-skip otomatis bila env tidak ada.
- `TEST_DATABASE_URL` di `.env.example` (placeholder password).

## [0.1.0] - 2026-11-08

### Added
- Inisialisasi repository dan salinan dokumen planning di `docs/`.
