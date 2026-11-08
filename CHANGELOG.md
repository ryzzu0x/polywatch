# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Added — T-019 (health monitor)
- `application/use_cases/health_monitor.py`: deteksi komponen basi — trade
  terakhir market LIVE (port baru `HealthActivityRepository`,
  `SqlAlchemyTradeRepository.last_trade_at_live`), siklus discover/result
  scheduler terakhir (dilaporkan via `record_cycle` dari loop; hanya siklus
  sukses dihitung aktivitas). Ambang `STALE_INGEST_S` (120) /
  `STALE_DISCOVER_S` (180); rate limit 1 notifikasi per jenis per
  `HEALTH_RATE_LIMIT_S` (600 dtk) supaya tidak spam saat gangguan
  berjam-jam; notifikasi pemulihan saat aktif kembali; grace period
  `HEALTH_GRACE_S` (300) saat start tanpa data — tidak langsung alarm.
  Semua state in-memory (ADR-008), notifikasi keluar lewat port `Notifier`.
- Scheduler melaporkan tiap siklus (ok/gagal) ke monitor; laporan gagal
  tidak mengganggu loop. Runner menjalankan `_health_loop` tiap
  `HEALTH_CHECK_INTERVAL_S` (30) — no-op bila monitor tidak terpasang.
  `Container.setup_health` merakit monitor dengan TelegramNotifier owner.
  Notifikasi gagal kirim / check gagal hanya di-log. 297 → 316 test.

### Added — T-020 (backup/restore)
- `scripts/backup.sh`: `pg_dump` format custom (`-Fc`) ke
  `${BACKUP_DIR}/<slug>-YYYYmmdd-HHMMSS.dump`, koneksi dari `APP_DB_URL`
  atau `PG*` (tanpa nilai hardcoded), rotasi 14 hari
  (`RETENTION_DAYS`, `find -mtime +14 -delete`), dump gagal di tengah →
  file `.partial` dihapus + exit non-zero, `set -euo pipefail`.
- `scripts/restore.sh`: `pg_restore` ke database target eksplisit
  (default DB uji via `APP_DB_URL`/`PGDATABASE`/argumen), konfirmasi
  interaktif wajib (ketik `RESTORE`) atau `--yes`, menolak restore ke
  database produksi (`PROD_DATABASE`, default `polywatch_db`) tanpa
  `--force-prod`.
- `README.md`: bagian cara pakai backup/restore + jadwal cron contoh
  tanpa path server nyata. Diverifikasi nyata: backup `polywatch_db` →
  restore ke `polywatch_restore_test` → 12 tabel, jumlah baris markets
  & trades cocok.

### Added — T-018 (E2E + siklus penuh)
- `tests/e2e/test_core_flow.py`: E2E alur inti dengan DB nyata `polywatch_test`
  + semua sumber palsu — discover_markets, ingest_trades (fake WS + fake RPC
  yang mem-decode receipt fixture nyata), hook `on_saved` → `evaluate_alert`
  (notifier palsu menerima tepat 1 pesan karena cooldown, `alerts_sent`
  mencatat `delay_ms`), `record_result` (market_results + status resolved),
  `recompute_scores` (wallet_stats benar & idempoten). Jalur gagal: wallet
  market maker tidak memicu alert; market belum resolved tidak menghasilkan
  result palsu. Bersih-bersih sendiri, auto-skip tanpa `TEST_DATABASE_URL`.
- `main/scheduler.py`: `discover_loop` (sinkronkan market live) dan
  `result_loop` (market CLOSED tanpa hasil → `record_result`, market yang baru
  resolved → `recompute_scores`). Exception satu siklus ditangkap + di-log
  terstruktur tanpa mematikan loop; shutdown rapi lewat `stop_event`/`cancel_all`.
- `Settings`: `DISCOVER_INTERVAL_S` (30), `RESULT_INTERVAL_S` (30),
  `HEARTBEAT_INTERVAL_S` (60). `Container.setup_scheduler()` merakit use case
  siklus penuh; runner menjalankan scheduler bersama bot polling + heartbeat +
  hook ingest. `--check` tetap tanpa efek samping. 291 → 297 test.

### Added — T-016, T-017 (wallet & alert)
- `/top [n]` (default 10, maks 25): wallet skor tertinggi dari `wallet_stats`
  tanpa market maker. `/wallet <alamat>`: skor, edge, ROI, PnL, jumlah market,
  win rate, status MM dan watchlist; alamat divalidasi `normalize_wallet`.
- `/watch` / `/unwatch <alamat>`: kelola `watchlist` lewat port baru
  `WatchlistRepository`, idempoten.
- Use case `evaluate_alert` (dipanggil dari hook `on_saved`): wajib
  `is_alertable`, skor ≥ min_score (wallet watchlist tetap terkirim ber-prefix
  `[WATCHLIST]`), size ≥ min_usdc, cooldown per (user, wallet) default 300 dtk;
  setiap alert dicatat ke `alerts_sent` dengan `delay_ms`.
- Formatter alert Bahasa Indonesia: market slug, wallet pendek, skor & edge,
  aksi beli/jual Up/Down, size USDC, harga, link market.
- `/alerts`: tampil & ubah `alert_settings` lewat tombol inline (skor
  50/60/70/80, size $50/$100/$500, cooldown 1/5/15 mnt, on/off).
- `Container.setup_ingest` merakit `on_saved → evaluate_alert`. 254 → 291 test.

### Added — T-014, T-015 (bot Telegram)
- Bot aiogram 3: middleware guard owner-only, `/start`, `/status` (market live,
  jumlah per status, trade 24 jam, versi) lewat port `MarketStatsRepository`/
  `TradeStatsRepository`.
- `/markets`: daftar aset × timeframe dari `watch_config` + tombol inline
  toggle (idempoten, edit pesan).
- `/results <aset> <5m|15m> [n]`: hasil terakhir dari `market_results`, validasi
  domain, limit 1–20.
- Runner menjalankan long polling + heartbeat + hook ingest dalam satu proses,
  shutdown rapi SIGINT/SIGTERM. `--check` tidak menyentuh Telegram.
- Runtime nyata: `getMe` @polywatchryzzubot sukses, polling aktif, SIGTERM →
  shutdown bersih. 231 → 254 test.

### Added — T-005c (spike)
- `docs/spikes/T-005c-orderfilled-rpc.md` + `scripts/spike/t005c_orderfilled_probe.py`:
  decode `OrderFilled` CTF Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`)
  dari `eth_getTransactionReceipt` memberi maker/taker wallet + tokenId + jumlah
  + arah; e2e WS→tersimpan median 855 ms / p90 1.683 ms / max 2.128 ms (39 trade)
  vs Data API median 164 s.

### Added — T-011 (ingest hybrid)
- `orderbook_ws.py`: klien WS CLOB market channel, reconnect + backoff (close 1000
  "all subscribed assets resolved"), heartbeat PING/PONG, parse `last_trade_price`.
- `polygon_rpc.py`: JSON-RPC Polygon async dengan failover (Alchemy → publicnode →
  1rpc → drpc) + poll receipt pra-konfirmasi.
- `order_filled_decoder.py`: decode `OrderFilled` V2 manual tanpa library ABI.
- `trade_stream.py`: adapter ke port `TradeEventStream`/`ReceiptFetcher`.
- `use_cases/ingest_trades.py`: subscribe token market LIVE enabled, dedupe tx_hash,
  decode fill → `Trade`, simpan idempoten, hook `on_saved`, `backfill(market_id, since)`
  lewat Data API.
- `TradeRepository.insert_many`/`last_traded_at` (ON CONFLICT (tx_hash, log_index,
  wallet) DO NOTHING), `MarketRepository.get_token_market`, `LiveTokenQuery`.

### Changed — T-011
- ADR-004 direvisi: ingest trade MVP hybrid WS+RPC (OrderFilled); Data API menjadi
  jalur backfill, bukan polling utama.

### Added — T-012, T-013 (skor wallet)
- Domain scoring (ADR-003): `ScoringPolicy` injectable, `edge_shrunk`/`roi_shrunk`
  (nilai × n/(n+k)), `score_wallet` 0–100 ter-clamp, `aggregate_market` +
  `build_wallet_stats`, filter market maker (dua sisi ≥ 30% market → skor 0).
- Use case `recompute_scores` (hitung ulang dari seluruh market resolved) +
  `WalletStatsRepository` PostgreSQL upsert `ON CONFLICT (wallet)`.

### Added — T-010 (domain posisi)
- `apply_trade` (BUY tambah net+cost, SELL kurangi net, tolak oversell via
  `OversellError`), `positions_from_trades`, `realize` (PnL menang 1/kalah 0),
  `average_entry` berbobot size.

### Added — T-009 (hasil market)
- `PolymarketPriceSource` (crypto-price/Chainlink, window >10 mnt dilewati) +
  `BinancePriceSource` (klines 1m cadangan).
- Use case `record_result`: poll resolusi Gamma tiap 30 s, arah hasil hanya dari
  `outcomePrices`, simpan `market_results` idempoten, transisi CLOSED→RESOLVED,
  timeout 15 mnt → notifikasi owner sekali per market.
- `TelegramNotifier` (adapter notifikasi, bukan handler bot).

### Added — T-008 (pengaturan market)
- `SqlAlchemyWatchConfigRepository` + `SqlAlchemyUserRepository` (upsert idempoten,
  get-or-create user).
- Use case `manage_settings`: `set_watch_enabled`/`list_watch`/`enabled_assets`;
  filter `enabled_assets` menghentikan market yang dinonaktifkan di siklus berikutnya.

### Added — T-007 (discovery market)
- `GammaMarketSource`: httpx async, retry + backoff + jitter, jendela
  `end_date_min=now-1j`/`end_date_max=now+3j`, offset dibatasi 2000, parsing
  `clobTokenIds`/`outcomes` tahan error.
- Use case `discover_markets`: auto-detect aset/timeframe dari slug, upsert
  idempoten, log new/total/skipped.
- `SqlAlchemyMarketRepository`: upsert `ON CONFLICT (condition_id)`, asset auto-create,
  status tidak pernah mundur. Verifikasi live: 2× discover = 24 market, 8 aset, 0 duplikat.

### Added — T-006 (domain market)
- Entity `MarketResult`, aturan transisi `upcoming → live → closed → resolved`
  (transisi mundur/melompat ditolak), pemetaan `outcomePrices` dan validasi
  `umaResolutionStatus`.

### Added — T-005 (spike sumber data)
- Laporan `docs/spikes/T-005-sumber-data.md` + contoh respons + `scripts/spike/t005_probe.py`.
- ADR-004 Diterima (Data API v2 `?condition=`, cursor-only, latency median 164 s /
  p90 251 s) dan ADR-005 Diterima (open/close: Polymarket crypto-price utama,
  Binance 1m cadangan, akurasi 41/48).

### Added — fondasi (T-001…T-004)
- Struktur Clean Architecture (`domain`, `application`, `infrastructure`,
  `presentation`, `main`), config env tervalidasi, logging JSON + redaksi secret,
  entrypoint CLI.
- Domain awal: `MarketSlug`, `Timeframe`, `Asset`, `Outcome`, `Trade`, `Position`,
  `WalletStats`; port/interface sumber, repository, notifier, clock.
- PostgreSQL lokal (role `polywatch_app`, DB `polywatch_db`/`polywatch_test`),
  11 tabel MVP via Alembic (`5dee4322c846`), model SQLAlchemy 2 async.
- Runner heartbeat (port `Clock`, SIGINT/SIGTERM), composition root lengkap.
- Tooling: uv, ruff, mypy strict, import-linter, pytest, CI GitHub Actions.

## [0.1.0] - 2026-11-08

### Added
- Inisialisasi repository dan salinan dokumen planning di `docs/`.
