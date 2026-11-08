# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Added (T-018)
- `tests/e2e/test_core_flow.py`: E2E alur inti dengan DB nyata
  `polywatch_test` + semua sumber palsu (tanpa jaringan) —
  discover_markets (market live/upcoming + aset auto-detect dari slug),
  ingest_trades dengan fake WS + fake RPC yang me-decode receipt fixture
  nyata `docs/spikes` (trade tersimpan idempoten), hook `on_saved` →
  `evaluate_alert` (notifier palsu menerima tepat 1 pesan karena cooldown,
  `alerts_sent` tercatat dengan `delay_ms` wajar), `record_result`
  (market_results + status resolved), `recompute_scores` (wallet_stats
  benar & idempoten), plus jalur gagal: wallet market maker tidak
  memicu alert; market belum resolved tidak menghasilkan result palsu.
  Bersih-bersih sendiri (truncate sebelum & sesudah), auto-skip tanpa
  `TEST_DATABASE_URL`.
- `main/scheduler.py`: task periodik asyncio untuk siklus penuh —
  `discover_loop` (sinkronkan market live) dan `result_loop` (market
  CLOSED tanpa hasil → `record_result`, market yang baru resolved →
  `recompute_scores`). Exception satu siklus ditangkap + di-log
  terstruktur (`scheduler_cycle_error`) tanpa mematikan loop; shutdown
  rapi lewat `stop_event`/`cancel_all`.
- `Settings`: field baru `DISCOVER_INTERVAL_S` (default 30),
  `RESULT_INTERVAL_S` (default 30), `HEARTBEAT_INTERVAL_S` (default 60);
  CLI `--heartbeat-interval` kembali opsional (default dari settings).
- `Container.setup_scheduler()`: merakit use case siklus penuh (Gamma
  market source, harga Polymarket + fallback Binance); runner
  menjalankan scheduler bersama bot polling + heartbeat + hook ingest —
  deploy tidak lagi menjalankan "bot kosong". `--check` tetap tidak
  menjalankan apa pun dan tidak menyentuh Telegram. 291 → 297 test.

### Added (T-016, T-017)
- `/top [n]` (default 10, maks 25): wallet skor tertinggi dari
  `wallet_stats` tanpa market maker — skor, edge, ROI, jumlah market,
  win rate. `/wallet <alamat>`: statistik satu wallet (skor, edge, ROI,
  PnL, jumlah market, win rate, status MM, status watchlist); alamat
  divalidasi `normalize_wallet` dengan pesan ramah saat salah format.
- `/watch` / `/unwatch <alamat>`: kelola tabel `watchlist` lewat port
  baru `WatchlistRepository` (add/remove/list/is_watched), idempoten
  dengan balasan singkat.
- Use case `evaluate_alert` (dipanggil dari hook `on_saved`
  `ingest_trades`): cek `is_alertable` (sampel cukup, bukan market
  maker), skor ≥ min_score (wallet di watchlist tetap terkirim dengan
  prefix `[WATCHLIST]`), size ≥ min_usdc, cooldown per (user, wallet)
  default 300 dtk; setiap alert dicatat ke `alerts_sent` dengan
  `delay_ms` = selisih `traded_at` → waktu kirim.
- Formatter alert Bahasa Indonesia ringkas: market slug, wallet pendek
  `0xabab…abab`, skor & edge wallet, aksi beli/jual Up/Down, size USDC,
  harga, link market.
- `/alerts`: tampil & ubah pengaturan alert (`alert_settings`) dengan
  tombol inline — skor 50/60/70/80, size $50/$100/$500, cooldown
  1/5/15 mnt, on/off — lewat use case `manage_alerts` + port
  `AlertSettingsRepository`/`AlertsSentRepository` (impl PostgreSQL).
- Container merakit hook `on_saved → evaluate_alert` (`setup_ingest`);
  tanpa hook, perilaku ingest/runner tidak berubah. 254 → 291 test.

### Added (T-014, T-015)
- Bot Telegram aiogram 3: middleware guard owner-only (chat selain
  owner ditolak satu baris generik), `/start`, `/status` (market live
  terakhir, jumlah market per status, trade 24 jam terakhir, status
  WS/RPC, versi) lewat use case `bot_status` + port baru
  `MarketStatsRepository`/`TradeStatsRepository` (interface segregation).
- `/markets`: daftar aset × timeframe dari `watch_config` + tombol
  inline toggle (callback → `set_watch_enabled` nilai kebalikan, edit
  pesan idempoten + konfirmasi singkat).
- `/results <aset> <5m|15m> [n]`: hasil market terakhir dari
  `market_results` lewat use case `list_results`; validasi aset/
  timeframe lewat domain, limit 1–20, pesan ramah saat data kosong.
- Runner menjalankan long polling bot + heartbeat + hook ingest dalam
  satu proses; shutdown rapi SIGINT/SIGTERM (semua task dibatalkan,
  session Bot + engine DB ditutup). `--check` tetap tanpa Telegram.
- Runtime nyata: `getMe` @polywatchryzzubot sukses, polling aktif,
  SIGTERM → shutdown bersih. 231 → 254 test hijau.

### Added (T-005c)
- Spike `docs/spikes/T-005c-orderfilled-rpc.md` + probe
  `scripts/spike/t005c_orderfilled_probe.py`: bukti decode `OrderFilled`
  CTF Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`) dari
  `eth_getTransactionReceipt` memberi maker/taker wallet + tokenId +
  jumlah + arah; e2e WS→tersimpan median 855 ms / p90 1.683 ms /
  max 2.128 ms (39 trade) vs Data API median 164 s.

### Added (T-011)
- `infrastructure/polymarket/orderbook_ws.py`: klien WS CLOB market
  channel dengan reconnect otomatis + backoff (close 1000
  "all subscribed assets resolved" tiap siklus market), heartbeat
  PING/PONG, parse event `last_trade_price`.
- `infrastructure/polymarket/polygon_rpc.py`: klien JSON-RPC Polygon
  async dengan failover endpoint berurutan (Alchemy bila di-set →
  publicnode → 1rpc → drpc) + poll receipt pra-konfirmasi (event WS
  datang sebelum tx masuk blok).
- `infrastructure/polymarket/order_filled_decoder.py`: decode log
  `OrderFilled` V2 manual (7 word statis) tanpa library ABI.
- `infrastructure/polymarket/trade_stream.py`: adapter ke port
  `TradeEventStream`/`ReceiptFetcher`.
- `application/use_cases/ingest_trades.py`: orkestrasi ingest hybrid —
  subscribe token market LIVE yang enabled, dedupe tx_hash in-memory,
  decode fill → `Trade` (mapping token via `token_up`/`token_down`),
  simpan idempoten, hook `on_saved` untuk alert T-017, `backfill(
  market_id, since)` lewat Data API.
- `TradeRepository.insert_many`/`last_traded_at` (PostgreSQL,
  ON CONFLICT (tx_hash, log_index, wallet) DO NOTHING),
  `MarketRepository.get_token_market`, `LiveTokenQuery`
  (markets LIVE × watch_config enabled).
- Port `TradeEventStream`/`ReceiptFetcher`; DTO `TradeEventSnapshot`/
  `FilledOrderSnapshot`/`TokenMarketInfo`.
- Test: decoder (fixture receipt nyata), failover RPC (respx),
  dedupe & backfill (fake port), integration insert_many idempoten
  (auto-skip tanpa `TEST_DATABASE_URL`).

### Changed (T-011)
- ADR-004 direvisi: ingest trade MVP kini hybrid WS+RPC (OrderFilled),
  Data API menjadi jalur backfill — bukan polling utama.


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

### Added (T-016, T-017)
- `/top [n]` (default 10, maks 25): wallet skor tertinggi dari
  `wallet_stats` tanpa market maker — skor, edge, ROI, jumlah market,
  win rate. `/wallet <alamat>`: statistik satu wallet (skor, edge, ROI,
  PnL, jumlah market, win rate, status MM, status watchlist); alamat
  divalidasi `normalize_wallet` dengan pesan ramah saat salah format.
- `/watch` / `/unwatch <alamat>`: kelola tabel `watchlist` lewat port
  baru `WatchlistRepository` (add/remove/list/is_watched), idempoten
  dengan balasan singkat.
- Use case `evaluate_alert` (dipanggil dari hook `on_saved`
  `ingest_trades`): cek `is_alertable` (sampel cukup, bukan market
  maker), skor ≥ min_score (wallet di watchlist tetap terkirim dengan
  prefix `[WATCHLIST]`), size ≥ min_usdc, cooldown per (user, wallet)
  default 300 dtk; setiap alert dicatat ke `alerts_sent` dengan
  `delay_ms` = selisih `traded_at` → waktu kirim.
- Formatter alert Bahasa Indonesia ringkas: market slug, wallet pendek
  `0xabab…abab`, skor & edge wallet, aksi beli/jual Up/Down, size USDC,
  harga, link market.
- `/alerts`: tampil & ubah pengaturan alert (`alert_settings`) dengan
  tombol inline — skor 50/60/70/80, size $50/$100/$500, cooldown
  1/5/15 mnt, on/off — lewat use case `manage_alerts` + port
  `AlertSettingsRepository`/`AlertsSentRepository` (impl PostgreSQL).
- Container merakit hook `on_saved → evaluate_alert` (`setup_ingest`);
  tanpa hook, perilaku ingest/runner tidak berubah. 254 → 291 test.

### Added (T-014, T-015)
- Bot Telegram aiogram 3: middleware guard owner-only (chat selain
  owner ditolak satu baris generik), `/start`, `/status` (market live
  terakhir, jumlah market per status, trade 24 jam terakhir, status
  WS/RPC, versi) lewat use case `bot_status` + port baru
  `MarketStatsRepository`/`TradeStatsRepository` (interface segregation).
- `/markets`: daftar aset × timeframe dari `watch_config` + tombol
  inline toggle (callback → `set_watch_enabled` nilai kebalikan, edit
  pesan idempoten + konfirmasi singkat).
- `/results <aset> <5m|15m> [n]`: hasil market terakhir dari
  `market_results` lewat use case `list_results`; validasi aset/
  timeframe lewat domain, limit 1–20, pesan ramah saat data kosong.
- Runner menjalankan long polling bot + heartbeat + hook ingest dalam
  satu proses; shutdown rapi SIGINT/SIGTERM (semua task dibatalkan,
  session Bot + engine DB ditutup). `--check` tetap tanpa Telegram.
- Runtime nyata: `getMe` @polywatchryzzubot sukses, polling aktif,
  SIGTERM → shutdown bersih. 231 → 254 test hijau.

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
