# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Fixed — T-031 (BUG-015: session ingest keracun `PendingRollbackError` + ADR-018)
- **BUG-015 (critical):** soak test T-030 menunjukkan 14× `ingest_stream_error`
  berantai — `sqlalchemy.exc.PendingRollbackError: Can't reconnect until
  invalid transaction is rolled back` dari `_process_tx → insert_many →
  session.execute`. Loop ingest memakai SATU scoped session seumur proses;
  satu kegagalan DB meninggalkan transaksi invalid yang meracuni SEMUA
  penyimpanan berikutnya sampai restart (stream restart tiap ~35 dtk,
  `trades` beku, token stream tidak pernah berubah walau jendela berganti).
  **Perbaikan (ADR-017):** (1) `BatchSessionTradeRepository` — session BARU
  dari factory untuk setiap `insert_many`, commit saat sukses, rollback +
  close selalu (unit-of-work per batch); container memasangnya untuk ingest.
  (2) Isolasi kegagalan di use case: `_insert_batch` menangkap exception,
  me-log `ingest_batch_error` terstruktur rate-limited + heartbeat not-ok,
  stream LANJUT ke tx berikutnya — satu batch gagal tidak mematikan stream.
  (3) Log `ingest_resubscribe` kini mencatat token lama vs baru
  (`tokens_old`/`tokens_new`/`tokens_added`/`tokens_removed`) — pertukaran
  set token saat jendela 5m/15m berganti bisa diaudit.
- **Test regresi:** insert gagal sekali → batch berikutnya tetap tersimpan;
  stream tetap satu koneksi saat insert selalu gagal; session gagal →
  rollback + close tidak menular; integration DB: constraint violation →
  session baru tetap sukses. 427 → 435 test.

### Changed — T-031 (MIN_SAMPLE_MARKETS=10, ADR-018)
- `MIN_SAMPLE_MARKETS` dan default `ScoringPolicy.min_sample` 50 → **10**
  untuk MVP: data nyata menunjukkan 0 dari 1.891 wallet mencapai 50 market
  resolve (65 ≥ 10) sehingga alert otomatis mustahil menyala. `shrinkage_k`
  **tetap 50** — nilai efektif tetap disusutkan n/(n+k) (edge 20%: n=5 →
  1,8%; n=100 → 13,3%) sehingga sampel kecil otomatis diredam. Kembali ke
  50 saat v1/v2 (uang sungguhan); parameter tetap injectable lewat
  `ScoringPolicy`. Penjelasan untuk owner: `docs/CATATAN.md`.


### Fixed — T-029 (siklus lambat + alarm basi: paralelisasi, batch, ambang adaptif)
- **Siklus discover 4–5 menit → < 21 detik:** fetch slug Gamma kini
  paralel lewat `asyncio.gather` + `asyncio.Semaphore`
  (`GAMMA_MAX_CONCURRENCY=5`); throttle adapter Gamma dibuat async
  (`asyncio.sleep`, dipanggil di `_get_markets_page` — `time.sleep`
  lama memblokir event loop sehingga gather tetap sekuensial, dan
  throttle sebelumnya dead code); `fetch_delay_s` use case 0. Cache
  negatif aset mati (TTL 10 menit) — aset tanpa market (xag/xau)
  tidak di-query ulang.
- **Siklus result 128,9 detik → < 10 detik:** `RESULT_BATCH_SIZE`
  (default 10) membatasi market CLOSED per siklus; `record_result`
  mempoll tiap market sekali per siklus — belum resolved ditunda ke
  siklus berikutnya (dulu menunggu polling resolusi Gamma di dalam
  siklus); timeout dinilai dari umur market (`end_at` + `timeout_sec`);
  sweep dibatasi batch (default 50 kandidat).
- **Alarm basi (BUG-011) — ambang adaptif:** `STALE_DISCOVER_S`/
  `STALE_RESULT_S` (default 300) kini floor; ambang efektif =
  `max(3x durasi siklus terukur, floor)`; `record_cycle(kind, ok,
  duration_s)`. Interval scheduler default 30 → 60 dtk.
- **Observabilitas:** `duration_ms` di log semua siklus
  (`scheduler_cycle_duration`, `record_result_cycle`,
  `market_sweep_done`, `discover_markets selesai`).
- Bukti run nyata (satu instance, `timeout 600`, 16:30–16:40Z):
  discover max 20.389 ms, result max 9.204 ms (17 siklus);
  `health_stale` = 0 sepanjang run; market LIVE `end_at > now()` > 0
  sepanjang run; `trades` 9.347 → 19.215 (+9.868); `market_results`
  683 → 717 (+34). 393 → 415 test.

### Fixed — T-028 (market tidak mengalir: status beku + ingest market selesai + banjir log Gamma)
- **Bug 1 (status market tidak pernah maju):** seluruh 38 market
  berstatus LIVE sudah lewat `end_at` (`status='LIVE' and end_at >
  now()` = 0 baris) dan 23 dari 55 UPCOMING juga sudah lewat —
  `record_result` (hanya memproses CLOSED) tidak pernah menemukan
  market untuk di-resolve. **Perbaikan (ADR-015):** use case baru
  `SweepMarketStatus` — batch idempoten tiap siklus result yang
  menyelaraskan status dengan jendela waktu memakai aturan domain
  `status_at`: UPCOMING/LIVE dengan `end_at < now` → CLOSED, dan
  UPCOMING yang jendelanya berjalan → LIVE (siklus discover penuh
  bisa >5 menit; tanpa arah kedua market baru tak pernah LIVE).
  Port baru `MarketRepository.list_stale_open` + DTO `StaleMarket`.
- **Bug 2 (ingest subscribe market yang sudah selesai):**
  `SqlAlchemyLiveTokenQuery` memilih token dari `status='LIVE'` yang
  basi → WS tidak pernah mengirim trade (`trades` beku).
  **Perbaikan (ADR-015):** query berbasis jendela waktu —
  `start_at - 5 dtk <= now < end_at` dan status ≠ RESOLVED; market
  LIVE basi tidak diambil, market aktif walau statusnya masih
  UPCOMING tetap diambil (margin early-subscribe 5 dtk).
- **Bug 3 (8.000 WARNING per siklus):** suplemen list-endpoint Gamma
  mengembalikan market non-Up/Down (`wsl2-wol-nfo-...`) sampai offset
  1900 → ~8.000 WARNING `market Gamma di-skip: payload rusak` per
  siklus discover membanjiri journald. **Perbaikan:** suplemen
  diputus dari jalur normal (`supplement_source` opt-in; container
  tidak memasangnya — sumber kebenaran slug per jendela, ADR-010);
  log `payload rusak` diturunkan ke DEBUG dengan rate-limit 60 dtk.
- Bukti run nyata (satu instance, `timeout 420`, 15:24–15:31Z):
  sweep `closed=7 started=7` + `closed=8 started=8`;
  `mulai ingest ws tokens=26 markets=13`; `trades` 5.341 → 8.243
  (+2.902); `market_results` 549 → 580 (+31); 0 WARNING payload
  rusak. 376 → 393 test.

### Fixed — T-027 (alert palsu health monitor + instance ganda)
- **Bug 1 (dua instance berjalan bersamaan):** dua HealthMonitor
  mengirim alert ganda ke owner dan polling Telegram saling mengganggu
  (`TelegramConflictError: terminated by other getUpdates request`,
  log lama tryings=391). **Perbaikan (ADR-014):** single-instance lock
  `fcntl.flock` eksklusif non-blocking pada `LOCK_FILE` (default temp
  dir) saat start — lock kedua gagal → log `another_instance_running`
  + exit code 3 tanpa menyentuh Telegram/DB; flock dilepas kernel
  otomatis saat proses mati.
- **Bug 2 (urutan perakitan):** `main/cli.py` memanggil
  `setup_scheduler()` SEBELUM `setup_health()` — saat task scheduler
  dibuat `container.health_monitor` masih `None` →
  `create_scheduler_tasks(monitor=None)` → `record_cycle` tidak pernah
  dipanggil → discover/result selamanya dinilai "belum ada aktivitas"
  → alarm palsu. **Perbaikan:** urutan dibalik — `setup_health()`
  sebelum `setup_scheduler()`/`setup_ingest()`; test kontrak baru
  membuktikan `record_cycle` benar-benar terpanggil lewat container
  nyata dengan urutan CLI.
- **Bug 3 (metrik kesegaran ingest salah):** ingest diukur dari
  `max(traded_at)` market LIVE (`last_trade_at_live`) — jendela 5m baru
  tanpa trade atau pasar sepi → `None` → alarm padahal ingest sehat;
  ambang 120 dtk terlalu ketat. **Perbaikan (ADR-014):** kesegaran
  ingest = heartbeat — `IngestTrades.on_heartbeat` melaporkan
  `record_cycle("ingest", ok)` saat langganan WS dimulai, per event WS
  (throttle 60 dtk), dan tiap akhir siklus stream; ambang
  `STALE_INGEST_S` 120→600 dtk. Alarm ingest hanya bila ada market
  LIVE (`live_market_count() > 0`, method baru port
  `HealthActivityRepository`); pesan menyebut angka nyata ("tidak ada
  heartbeat ingest 10 menit (batch terakhir: ..., market live: 17)");
  `last_trade_at_live` menjadi info tambahan, bukan pemicu.
- **Anti-spam antar-restart:** state terakhir notifikasi disimpan di
  `HEALTH_STATE_FILE` (JSON kecil, tulis atomic) — restart berulang
  dalam cooldown 10 menit tidak mengirim ulang alarm (ADR-014).
- **Noise log:** logger `httpx`/`httpcore` di-redam ke WARNING —
  sebelumnya tiap request Gamma/RPC ditulis INFO dan membanjiri
  journald.
- Bukti run nyata (satu instance, 6.4 menit): 0 `health_stale` ke
  Telegram, ingest aktif (90 token / 45 market LIVE), +1246 baris
  trade tersimpan. 356 → 376 test.
### Fixed — T-026 (ingest trade tidak pernah berjalan, `trades` tetap 0)
- **Bug 1 (semantik watch):** `SqlAlchemyLiveTokenQuery` INNER JOIN
  `watch_config enabled=true`, sedangkan tabel `watch_config` kosong
  (0 baris) → 0 token live → setiap siklus log `tidak ada token live;
  siklus dilewati` dan `trades` tetap 0 meski DB punya 104 market
  (23 LIVE). "Belum ada pengaturan" salah diperlakukan sebagai "semua
  dimatikan". **Perbaikan (ADR-012):** baris `watch_config` hanya
  menyimpan *override* — pasangan asetxtimeframe tanpa baris dianggap
  enabled. LiveTokenQuery memakai LEFT JOIN + `IS DISTINCT FROM FALSE`;
  `is_enabled` default True; method baru `effective_watch_all` dipakai
  `ManageSettings.list_watch`/`enabled_assets` sehingga `/markets`
  menampilkan seluruh kombinasi dengan status efektif. Tanpa migrasi.
- **Bug 2 (ingest putus tiap siklus + tidak pernah commit):** ingest
  dipolling per-siklus dengan commit hanya di akhir siklus — stream WS
  kontinyu tidak pernah "berakhir", jadi trade tidak pernah ter-commit;
  query token live juga memakai scoped session task ingest yang ditutup
  `release_session` di tengah siklus. **Perbaikan (ADR-013):**
  `IngestTrades.run()` adalah loop kontinyu seumur proses (langganan WS
  hidup, refresh token tiap 30 dtk lewat port `LiveTokenSource` dengan
  session sekali-pakai, stream restart dengan token terbaru saat server
  menutup koneksi, close 1000 per jendela), plus **commit per batch**
  (`on_batch_commit`). Runner menjalankan hook ingest sebagai task
  tunggal. Bonus: `setup_health` NameError (HealthMonitor hanya
  TYPE_CHECKING import, sisa T-025) diperbaiki dengan import lokal.
- **Bug 3 (deadlock PostgreSQL):** `DeadlockDetectedError` antar-loop
  discover vs record_result saat upsert baris market sama. **Perbaikan:**
  `_execute_with_deadlock_retry` pada upsert/`save_result`/
  `update_status` MarketRepository — 3 percobaan, backoff 0.2s×2^n,
  rollback tiap retry, log terstruktur `db_deadlock_retry`/
  `db_deadlock_exhausted`; deteksi via SQLSTATE 40P01.
- **Verifikasi runtime nyata** (`timeout 240 uv run python -u -m
  polywatch.main.cli`, DB lokal): log `mulai ingest ws` tokens=44 (> 0);
  `select count(*) from trades` = 3162 (> 0; run debug 110 dtk menangkap
  3159 trade dari 451 event); contoh trade tersimpan lengkap
  (wallet/outcome/price/size/usdc/market/tx); deadlock retry terbukti
  bekerja di log run nyata (`db_deadlock_retry` attempt 1 → siklus
  lanjut). alerts_sent masih 0 — wajar di awal: `wallet_stats` baru
  terisi setelah market resolve dan recompute_scores jalan.
- Test: 342 → 356 (default-enabled + override unit/integration, loop
  kontinyu, commit per batch, retry deadlock).

### Fixed — T-025 (siklus scheduler tidak pernah berjalan, `markets` tetap 0)
- **Bug:** Container menyimpan *fungsi coroutine* (`run_discover` dll)
  sedangkan scheduler memanggil `.execute()` lewat protocol — setiap siklus
  gagal `AttributeError` yang ditangkap `_run_cycle` dan hanya di-log
  (`scheduler_cycle_error`). Discovery, record_result, dan recompute tidak
  pernah berjalan; tabel `markets` tetap 0 meski bot hidup dan health
  monitor terus melaporkan komponen basi. Field container bertipe `Any`
  membuat mypy buta terhadap ketidakcocokan ini.
- **Perbaikan (ADR-011, kontrak siklus = callable coroutine):**
  `DiscoverCycle`/`ResultCycle`/`RecomputeCycle` menjadi type alias
  `Callable[[], Awaitable[...]]` (recompute menerima `market_id`);
  `discover_loop`/`result_loop`/`_run_cycle` memanggil callable langsung.
  Field `Container.discover_cycle`/`record_result_cycle`/`recompute_cycle`
  kini bertipe eksplisit dan `health_monitor: HealthMonitor | None` —
  mypy strict menangkap mismatch kontrak. Test kontrak baru
  (`tests/integration/test_scheduler_contract.py`) mengambil siklus dari
  container nyata (`setup_scheduler`) dan membuktikan loop berjalan tanpa
  `scheduler_cycle_error` dengan market tersimpan.
- **Verifikasi runtime nyata** (`timeout 260 uv run python -u -m polywatch.main.cli`,
  DB lokal): log `discover_markets selesai` (new_markets=18,
  total_markets=88); `select count(*) from markets` = 104 (23 LIVE,
  48 RESOLVED; sebelumnya 0); 0 `scheduler_cycle_error`; 17
  `market_result_saved` + 17 `recompute_scores_done`. Catatan: ditemukan
  pula deadlock PostgreSQL jarang antar-loop discover vs record_result
  (upsert baris market sama bersamaan) — diluar scope T-025, siklus
  berikutnya otomatis sukses; masuk backlog.

### Fixed — T-024 (discovery market melewatkan market live)
- **Bug:** `discover_markets` menyimpan 0 market Up/Down live. Bukti:
  `GET /markets?closed=false&order=endDate&ascending=true&end_date_min=<now-1h>`
  mengembalikan 100 item market jangka panjang (val-bon-..., itf-...) tanpa
  satu pun slug updown; sementara `end_date_max=<now+60m>` mengembalikan 54
  item updown yang semuanya market basi Des 2025 (tak pernah ditutup,
  volume 0). Market live hanya bisa ditemukan deterministik lewat
  `GET /markets?slug=<slug>` dengan slug jendela waktu.
- **Perbaikan (ADR-010, discovery berbasis slug per jendela waktu):**
  untuk tiap (aset, timeframe) dihitung window start =
  `floor(now/durasi)*durasi`, lalu market dicari per slug untuk jendela
  berjalan + 2 ke depan (`closed=false`) dan 3 ke belakang
  (`closed=true` — untuk `record_result`; market live hilang dari
  pencarian slug setelah end). Aset dari tabel `assets` (port `AssetSource`)
  ∪ default `Settings.MARKET_ASSETS`; timeframe domain; pembentukan slug
  (`window_start`/`build_slug`) di application layer — adapter Gamma hanya
  eksekusi HTTP dengan throttle ≤5 req/s. Jalur cadangan list-endpoint tetap
  ada sebagai supplement dan wajib lolos filter anti-stale: slug cocok pola
  updown DAN endDate ±6 jam dari sekarang (log `skipped_stale`) — market basi
  tidak akan pernah tersimpan lagi.
- **Verifikasi runtime nyata:** discovery sekali ke Gamma sungguhan menyimpan
  90 market di DB: 16 LIVE (8 aset × 5m/15m — sebanding 16 yang diukur
  manual), 32 UPCOMING, 42 CLOSED, 0 market basi.

### Fixed — T-023 (konkurensi session DB)
- **Bug:** `/markets` dan `/status` gagal saat update Telegram diproses
  bersamaan — `InvalidRequestError: This session is provisioning a new
  connection; concurrent operations are not permitted`. Penyebab:
  `main/container.py` merakit setiap use case dengan satu `AsyncSession`
  yang sama, lalu use case itu dipakai bersama oleh semua update
  konkuren (aiogram), scheduler, dan ingest; satu `AsyncSession` tidak
  boleh dipakai lintas task.
- **Perbaikan (ADR-009, akses DB per-task/per-update):** container kini
  memegang `async_scoped_session(async_sessionmaker,
  scopefunc=asyncio.current_task)`; siklus scheduler/ingest/health ambil
  session scoped lalu `commit` + `remove()` di `finally` tiap siklus.
  Handler bot memakai provider use case per-update
  (`Container._bot_providers` → `build_router` menerima instance atau
  provider): setiap update Telegram merakit use case dengan session DB
  sendiri; `SessionReleaseMiddleware` commit unit-of-work lalu remove
  session di `finally` — jalur error pun melepas session (tidak ada
  kebocoran koneksi; kegagalan commit/release di-log terstruktur
  `session_commit_error`/`session_release_error`).
- Use case tetap tidak tahu SQLAlchemy; container tetap composition root
  (tidak ada perubahan layer).
- Test regresi `tests/e2e/test_concurrent_updates.py`: 6 update konkuren
  (2× `/markets`, `/status`, `/top`, `/alerts`, callback toggle) via
  `asyncio.gather` atas `Dispatcher.feed_update` dengan DB nyata
  `polywatch_test` — sebelumnya mereproduksi error yang sama, kini semua
  sukses dan tulisan toggle ter-commit. 316 → 317 test.

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
