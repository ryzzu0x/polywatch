# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Added (T-009)
- `PolymarketPriceSource` (`infrastructure/prices/polymarket_price_source.py`):
  endpoint crypto-price (Chainlink, ADR-005), httpx async + retry/backoff/jitter
  + timeout; window market yang sudah lewat >10 menit dilewati tanpa request
  karena `eventStartTime` historis diabaikan server (T-005).
- `BinancePriceSource` (`infrastructure/prices/binance_price_source.py`):
  klines 1m sebagai sumber cadangan; open = open kline pertama window,
  close = close kline terakhir.
- Use case `record_result` (`application/use_cases/`): poll resolusi resmi Gamma
  tiap 30 s sampai `umaResolutionStatus=resolved`; arah hasil hanya dari
  `outcomePrices` (sumber kebenaran); open/close + `price_source` dari
  PriceSource (fallback Binance, sentinel `unavailable` bila semua gagal);
  simpan `market_results` idempoten (ON CONFLICT DO NOTHING) + transisi status
  CLOSED→RESOLVED via domain; timeout 15 menit → notifikasi owner sekali per
  market + log error terstruktur.
- `GammaMarketSource.get_result` kini memetakan `outcomePrices` ke `Outcome`.
- `MarketRepository` PostgreSQL: `get_result`/`save_result`/`update_status`/
  `has_result`/`list_pending_resolution`.
- `TelegramNotifier` (`infrastructure/telegram_notifier.py`): adapter notifikasi
  aiogram (bukan handler bot — itu T-014); error kirim di-log, tidak menggagalkan
  use case.

### Added (T-008)
- `SqlAlchemyWatchConfigRepository` + `SqlAlchemyUserRepository`
  (`infrastructure/db/repositories/`): upsert idempoten
  `ON CONFLICT uq_watch_config`, `users` get-or-create per telegram_id.
- Use case `manage_settings`: `set_watch_enabled`/`list_watch`/`enabled_assets`;
  validasi `Asset`/`Timeframe` domain; aset harus sudah terdaftar di `assets`;
  `enabled_assets` adalah filter siklus ingest — menonaktifkan aset×timeframe
  menghentikan pemrosesan market itu di siklus berikutnya (dibuktikan test).

### Added (T-007)
- `GammaMarketSource` (`infrastructure/polymarket/gamma_client.py`): httpx async
  dengan retry + exponential backoff + jitter, timeout eksplisit; polling market
  live sesuai T-005 (`closed=false&order=endDate&ascending=true`), jendela
  `end_date_min=now-1j`/`end_date_max=now+3j`, paginasi offset dibatasi 2000
  (Gamma menolak lebih). Parsing `clobTokenIds`/`outcomes` (string JSON) tahan
  error: market rusak di-log lalu di-skip. Filter timeframe lewat
  `MarketSlug.parse`.
- Use case `discover_markets` (`application/use_cases/`): auto-detect
  aset/timeframe dari slug, slug aneh di-skip tanpa menggagalkan batch, upsert
  idempoten lewat port `MarketRepository`, log terstruktur new/total/skipped.
- `SqlAlchemyMarketRepository` (`infrastructure/db/repositories/`): upsert
  `ON CONFLICT (condition_id)`, asset baru dari slug dibuat otomatis
  (idempoten), status market tidak pernah mundur saat upsert ulang.
- Verifikasi live: discover 2× berturut-turut = 24 market, 8 aset, 0 duplikat.

### Added (T-006)
- Entity `MarketResult` (open/close price `Decimal`, `Outcome`, volume, sumber
  harga, waktu resolusi) dengan equality/hash.
- Aturan transisi status market `upcoming → live → closed → resolved`
  (`can_transition`/`transition_status`/`status_at`); transisi mundur atau
  melompat ke resolved ditolak.
- Pemetaan `outcomePrices` Gamma (indeks 0 = Up) ke `Outcome` dan validasi
  `umaResolutionStatus`.

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

## [Unreleased]

### Added
- **T-012** — Domain scoring wallet (ADR-003): `ScoringPolicy` injectable,
  `edge_shrunk`/`roi_shrunk` (nilai × n/(n+k)), `score_wallet` 0-100 kombinasi
  edge & ROI ter-shrink (di-clamp, tanpa pembagian nol), `aggregate_market` +
  `build_wallet_stats` (rata-rata entry berbobot USDC, PnL, staked), filter
  market maker (dua sisi ≥ 30% market → skor 0), `win_rate_confidence`.
- **T-013** — Use case `recompute_scores`: hitung ulang statistik semua wallet
  yang trade di market yang baru resolve dari **seluruh** market resolve;
  `WalletStatsRepository` PostgreSQL (upsert `ON CONFLICT (wallet)`, `top`
  exclude market maker, `get`); port baru `TradeRepository.resolved_trades_for_wallet`
  + DTO `ResolvedTrade` (append-only, signature lama tidak berubah);
  `SqlAlchemyTradeRepository` bagian baca (tulis = scope T-011).

## [0.1.0] - 2026-11-08

### Added
- Inisialisasi repository dan salinan dokumen planning di `docs/`.
