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
- [x] **T-003 · Config env, composition root, logging JSON** — S
  - Validasi env (pydantic-settings), container, entrypoint, heartbeat
  - *Selesai jika:* app start dengan env valid, gagal jelas dengan env tidak lengkap
- [x] **T-004 · PostgreSQL lokal + Alembic + migrasi awal** — M
  - DB `polywatch_db`/`polywatch_test`, role `polywatch_app`, tabel sesuai ARCHITECTURE §4 (tanpa tabel v2)
  - *Selesai jika:* `alembic upgrade head` dan `downgrade base` berjalan bersih
- [x] **T-005 · Spike sumber data** — M
  - Data API: pagination/cursor, rate limit, latency trade → tersedia; field `proxyWallet`, sisi, outcome
  - Open/close price: bandingkan Chainlink vs sumber publik lain terhadap 50 resolusi resmi
  - *Selesai jika:* ADR-004 & ADR-005 ditulis dengan angka nyata
- [x] **T-005b · Spike CLOB WebSocket market channel** — M
  - Latency event `price_change`/`last_trade_price`, field identitas trade, ketahanan koneksi
  - *Selesai jika:* pernyataan tegas "cukup/tidak" untuk target ≤ 10 s dan kejelasan apakah WS membawa alamat wallet; ADR-004 diperbarui (`docs/spikes/T-005b-websocket.md`)
- [x] **T-005c · Spike OrderFilled → wallet via RPC Polygon** — M
  - WS `last_trade_price` → `transaction_hash` → `eth_getTransactionReceipt` → decode `OrderFilled` CTF Exchange V2 → alamat wallet; ukur latency end-to-end
  - *Selesai jika:* decode terbukti dengan tx nyata + latency median/p90/max ≥ 20 trade (`docs/spikes/T-005c-orderfilled-rpc.md`)

## M1 — Discovery Market & Arsip Hasil (F1, F2)

- [x] **T-006 · Domain Market/Timeframe/Outcome/MarketResult** — S
  - *Selesai jika:* unit test parsing slug & perhitungan waktu lulus
- [x] **T-007 · Gamma client + `discover_markets`** — M
  - Auto-detect aset dari slug, upsert idempoten
  - *Selesai jika:* integration test (fixture) lulus; 24 jam lokal tanpa market terlewat
- [x] **T-008 · `watch_config` + use case pengaturan market** — S
  - *Selesai jika:* menonaktifkan aset×tf menghentikan ingest siklus berikutnya (test)
- [x] **T-009 · PriceSource + `record_result`** — M
  - Open/close, result, volume, retry sampai resolved, alert jika > 15 mnt
  - *Selesai jika:* 50 sampel result = resolusi resmi; ≥ 99% lengkap dalam 2 mnt (verifikasi runtime ditunda ke QA T-022; jalur kode teruji unit+integration)

## M2 — Ingest Trade & Skor Wallet (F3, F4)

- [x] **T-010 · Domain Trade & Position** — S
  - `apply_trade` (BUY/SELL, tolak oversell), `positions_from_trades`, `realize` (PnL resolve), `average_entry` berbobot size; error domain `OversellError`
  - *Selesai jika:* unit test posisi bersih (buy/sell parsial, dua sisi) lulus
- [x] **T-011 · Ingest trades hybrid WS+RPC + backfill Data API** — M
  - `orderbook_ws` (reconnect, PING/PONG), `polygon_rpc` (failover Alchemy→publicnode→1rpc→drpc, poll receipt pra-konfirmasi), decoder `OrderFilled`, `ingest_trades` (dedupe tx_hash, insert_many idempoten ON CONFLICT, hook `on_saved` untuk alert), `backfill(market_id, since)` Data API
  - *Selesai jika:* unit+integration test lulus (decoder fixture receipt nyata, failover respx, insert_many idempoten 2×); verifikasi kecocokan 20 market ±2% vs Data API ditunda ke QA T-022
- [x] **T-012 · Domain scoring (edge, ROI, shrinkage, filter MM)** — M
  - *Selesai jika:* unit test kasus "beli 0.90 menang 85% → edge negatif" & deteksi dua sisi lulus
- [x] **T-013 · `recompute_scores` setelah resolve** — S
  - *Selesai jika:* wallet_stats terbarui ≤ 30 dtk setelah result tersimpan (integration test)

## M3 — Bot Telegram & Alert (F5)

- [x] **T-014 · Bot dasar + guard owner** — S
  - aiogram, `/start`, `/status`; chat selain owner ditolak
  - *Selesai jika:* test handler lulus; manual cek di Telegram
- [x] **T-015 · Perintah `/markets`, `/results`** — M
  - *Selesai jika:* skenario demo langkah 1–2 lulus
- [x] **T-016 · Perintah `/top`, `/wallet`, `/watch`, `/unwatch`** — M
  - *Selesai jika:* validasi alamat & output sesuai spesifikasi (test)
- [x] **T-017 · `evaluate_alert` + formatter + `/alerts`** — M
  - Ambang skor, size minimal, cooldown, harga sekarang, link market, catat `delay_ms`
  - *Selesai jika:* kriteria F5 lulus; median delay ≤ 10 dtk dalam 24 jam lokal (verifikasi 24 jam lokal ditunda ke QA T-022)
- [x] **T-018 · E2E core flow** — M
  - market → trade → alert → resolve → skor dengan sumber palsu + DB nyata
  - *Selesai jika:* test e2e hijau di CI

## M4 — Operasional, Deploy & Demo (F6)

- [x] **T-019 · Notifikasi error + heartbeat** — S
  - *Selesai jika:* ingest dimatikan > 2 mnt memicu notifikasi owner
- [x] **T-020 · Script backup/restore** — S
  - *Selesai jika:* restore ke DB uji berhasil
- [x] **T-023 · Fix konkurensi session DB (hotfix)** — S
  - *Selesai jika:* ≥5 update bot konkuren tanpa `InvalidRequestError` (test regresi DB nyata)
- [x] **T-024 · Discovery market berbasis slug per jendela waktu** — M
  - Deterministik: slug dari window start floor(now/durasi), jendela berjalan + 2 depan (closed=false), 3 belakang (closed=true) untuk record_result
  - Filter anti-market-basi: slug cocok pola updown DAN endDate ±6 jam dari sekarang; list-endpoint hanya supplement
  - *Selesai jika:* discovery live menyimpan market live > 0 (sebanding 16) di DB; market basi tidak masuk; gate hijau
- [x] **T-025 · Perbaiki kontrak siklus scheduler + hilangkan `Any` (hotfix)** — M
  - Satukan kontrak siklus: callable coroutine (ADR-011); tipe eksplisit di Container agar mypy menangkap ketidakcocokan
  - *Selesai jika:* runtime nyata `markets` > 0 tanpa `scheduler_cycle_error`; test kontrak lewat container nyata; gate hijau
- [x] **T-026 · Semantik watch default-enabled + ingest benar-benar jalan (hotfix)** — M
  - watch_config hanya menyimpan override: tabel kosong = semua aset x timeframe enabled (ADR-012); LiveTokenQuery LEFT JOIN; `/markets` konsisten via `effective_watch_all`
  - Ingest jadi loop kontinyu (ADR-013): langganan WS hidup seumur proses, refresh token tiap 30 dtk, commit per batch
  - Retry deadlock PostgreSQL (3x, backoff) pada upsert/save_result/update_status market + log terstruktur
  - *Selesai jika:* runtime nyata menemukan token > 0 dan `trades` > 0 dengan contoh trade tersimpan; gate hijau
- [x] **T-028 · Siklus hidup market & ingest berbasis jendela waktu (hotfix)** — M
  - Sweep status dua arah (ADR-015): market `end_at < now` yang masih UPCOMING/LIVE → CLOSED; UPCOMING yang jendelanya berjalan → LIVE — batch, idempoten, jalan tiap siklus result sebelum record_result
  - LiveTokenQuery berbasis jendela waktu: `start_at <= now < end_at` + status ≠ RESOLVED (margin early-subscribe 5 dtk), bukan status LIVE yang bisa basi
  - Suplemen list-endpoint Gamma diputus dari jalur normal (`supplement_source` kini opt-in, container tidak memasangnya); log "payload rusak" turun ke DEBUG + rate-limit 60 dtk
  - *Selesai jika:* run nyata `timeout 420` — log sweep > 0, `status='LIVE' and end_at > now()` > 0 saat jendela berjalan, `trades` dan `market_results` bertambah, nol WARNING payload rusak; gate hijau
- [ ] **T-021 · Deploy VPS (systemd, PostgreSQL, deploy.sh)** — M
  - Preflight port/unit/direktori, `.env` dibuat di VPS
  - *Selesai jika:* service jalan, `/status` hijau, satu siklus market penuh tercatat
- [ ] **T-022 · QA 7 hari + demo + tag v0.1.0** — S
  - *Selesai jika:* DoD MVP tercentang semua

---

## Backlog (Belum Dijadwalkan)

- [ ] v1: Paper copy-trade + laporan ROI harian/mingguan
- [x] (selesai di T-011, dibawa ke MVP) Ingest via event on-chain: WS detector + RPC OrderFilled — e2e median 855 ms
- [ ] v2: Multi-user (users, pengaturan per pengguna)
- [ ] v2: Generate wallet EVM otomatis saat `/start` + enkripsi private key
- [ ] v2: Deposit/withdraw USDC, approval, kredensial CLOB per pengguna
- [ ] v2: Eksekusi copy-trade otomatis + alert hasil eksekusi
- [ ] v2: Batas per trade/harian, stop-loss harian, kill switch, mode paper default
- [ ] v2: Ekspor private key oleh pengguna (dengan konfirmasi, pesan auto-hapus)

## Technical Debt (Dicatat Sadar)

| ID | Deskripsi | Alasan ditunda | Dicatat |
|---|---|---|---|
| TD-001 | (SELESAI via T-005c/T-011, 2026-11-08) Ingest pakai polling Data API, bukan stream on-chain | Kini hybrid WS+RPC OrderFilled; Data API jadi backfill | 2026-11-08 |

## Log Progres

| Tanggal | Task | Status | Catatan |
|---|---|---|---|
| 2026-11-08 | Planning | Draft | Dokumen planning v0.1 dibuat, menunggu review owner |
| 2026-11-08 | Planning | Disetujui | Owner menyetujui ADR-002 (Python) & ADR-007 (kustodian + ekspor key) |
| 2026-11-08 | T-001 | Selesai | Struktur Clean Architecture, config env tervalidasi, logging JSON, ruff/mypy strict/import-linter/pytest 27 test hijau |
| 2026-11-08 | T-002 | Selesai | Workflow CI `.github/workflows/ci.yml` (ruff, mypy, import-linter, pytest) |
| 2026-11-08 | T-003 | Selesai | Runner heartbeat (port Clock, SIGINT/SIGTERM), composition root engine/session DB lazy, redaksi secret di logging JSON, CLI default menjalankan runner; 44 test hijau; branch `feature/m0-fondasi` |
| 2026-11-08 | T-004 | Selesai | Role `polywatch_app` + DB lokal, model SQLAlchemy 2 async, Alembic (env baca Settings), migrasi awal 11 tabel MVP + index/unique §4; upgrade/downgrade/upgrade bersih; integration test via `TEST_DATABASE_URL` (auto-skip) |
| 2026-11-08 | T-005 | Selesai | Spike: Data API v1 pensiun (410) → v2 `?condition=` cursor-only, latency trade median 164 s/p90 251 s; ADR-004 Diterima; ADR-005: Polymarket crypto-price (Chainlink) utama + Binance cadangan (41/48); laporan `docs/spikes/T-005-sumber-data.md` |
| 2026-11-08 | T-006 | Selesai | Entity MarketResult, transisi status upcoming→live→closed→resolved (tolak mundur/lompat), pemetaan outcomePrices + validasi umaResolutionStatus; 25 unit test baru (73 total) |
| 2026-11-08 | T-007 | Selesai | GammaMarketSource (retry+backoff+jitter, parsing clobTokenIds/outcomes tahan error, jendela end_date now-1j..now+3j, offset ≤2000), use case discover_markets (auto-detect aset/timeframe dari slug), MarketRepository PostgreSQL (upsert ON CONFLICT condition_id, asset auto-create, status tak mundur); verifikasi live discover 2× = 24 market/8 aset/0 duplikat; 106 test hijau |
| 2026-11-08 | T-008 | Selesai | WatchConfigRepository PostgreSQL (upsert ON CONFLICT uq_watch_config) + UserRepository (get_or_create idempoten) + use case manage_settings (set_watch_enabled/list_watch/enabled_assets; validasi domain, aset harus terdaftar); test filter enabled membuktikan market yang dinonaktifkan tidak diproses siklus berikutnya |
| 2026-11-08 | T-009 | Selesai | PolymarketPriceSource (crypto-price Chainlink) + BinancePriceSource (klines 1m) cadangan, use case record_result (poll 30s, timeout 15 mnt → notifikasi owner sekali per market, outcome dari outcomePrices, price_source selalu terisi, idempoten), TelegramNotifier; verifikasi runtime 50 sampel ditunda ke QA T-022 |
| 2026-11-08 | T-005b | Selesai | Spike CLOB WS market channel: subscribe `{"assets_ids":[...],"type":"market"}` ok (PING/PONG); latency median ~81 ms / p90 ~85 ms / max 592 ms (49.422 event, 3 run 5,5 mnt); `last_trade_price` memuat tx_hash/price/size/side/timestamp TAPI TANPA alamat wallet → WS cukup untuk harga real-time & detector, wallet tetap lewat Data API/on-chain; close 1000 "all subscribed assets resolved" → reconnect wajib; ADR-004 diperbarui; laporan `docs/spikes/T-005b-websocket.md` |
| 2026-11-08 | T-010 | Selesai | Domain agregasi Trade → Position: `apply_trade` (BUY tambah net+cost, SELL kurangi net tanpa ubah cost, tolak oversell via `OversellError`), `positions_from_trades` per (wallet, market_id), `realize` PnL (menang 1/kalah 0), `average_entry` berbobot size (dua sisi → net terbesar); 22 unit test baru (167 total) |
| 2026-11-08 | T-012 | Selesai | ScoringPolicy injectable (min_sample/shrinkage_k/bobot edge-ROI/ambang), edge_shrunk & roi_shrunk = nilai×n/(n+k), score_wallet 0-100 di-clamp tanpa pembagian nol, aggregate_market+build_wallet_stats (rata-rata entry berbobot USDC, deteksi dua sisi ≥30% market → market maker skor 0); unit test kasus "beli 0.90 menang 85% → skor 0" lulus (24 test domain, 173 total hijau). Technical debt: helper agregasi posisi lokal di domain/scoring.py perlu disatukan dengan T-010 |
| 2026-11-08 | T-013 | Selesai | Use case recompute_scores (recompute penuh dari seluruh market resolve, bukan inkremental) + WalletStatsRepository PostgreSQL ON CONFLICT (wallet) idempoten + TradeRepository bagian baca (wallets_for_market, resolved_trades_for_wallet via port baru append-only); integration test polywatch_test verifikasi win_rate/edge/roi/score/is_market_maker + idempoten 2× eksekusi hasil sama; insert_many/last_traded_at = scope T-011 |
| 2026-11-08 | T-005c | Selesai | Spike OrderFilled: seluruh market updown live via CTF Exchange V2 `0xE111180000d2663C0091e4f400237545B87B996B`; event `OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)` topic0 `0xd543adfd...`; 39/39 trade ter-decode (maker/taker wallet, tokenId, size, usdc, fee, side) dan cocok event WS; e2e WS→tersimpan median 855 ms / p90 1.683 ms / max 2.128 ms (~190× lebih cepat dari Data API 164 s); temuan penting: event WS datang PRA-KONFIRMASI (receipt null sampai tx masuk blok → poll 250 ms); latensi RPC: Alchemy 7,5 ms, drpc 15,9 ms, publicnode 186 ms, 1rpc 269 ms; laporan `docs/spikes/T-005c-orderfilled-rpc.md` |
| 2026-11-08 | T-011 | Selesai | Ingest hybrid WS+RPC: `orderbook_ws.py` (subscribe market channel, reconnect+backoff otomatis atas close 1000 "all subscribed assets resolved", heartbeat PING), `polygon_rpc.py` (failover Alchemy→publicnode→1rpc→drpc, poll receipt pra-konfirmasi), `order_filled_decoder.py` (decode manual 7 word statis tanpa lib ABI), `ingest_trades.py` (dedupe tx_hash in-memory, mapping token via token_up/token_down, insert_many idempoten ON CONFLICT (tx_hash,log_index,wallet), hook on_saved untuk T-017, backfill Data API), TradeRepository.insert_many+last_traded_at PostgreSQL, LiveTokenQuery (markets LIVE × watch_config enabled); 36 test baru (231 total): decoder fixture receipt nyata, failover respx, dedupe, integration polywatch_test idempoten 2×; gate hijau (ruff/format/mypy strict 80 file/import-linter 3 kept) |
| 2026-11-08 | T-014 | Selesai | Bot aiogram 3: middleware guard owner-only (non-owner ditolak satu baris generik), /start (sapaan + daftar perintah), /status (market live terakhir, jumlah per status, trade 24 jam, status WS/RPC, versi) lewat use case BotStatus + port stats baru (interface segregation); runner menjalankan long polling + heartbeat + hook ingest bersamaan, shutdown rapi SIGINT/SIGTERM (semua task dibatalkan, session Bot + engine ditutup); --check tetap tanpa Telegram; runtime nyata: getMe @polywatchryzzubot sukses, polling aktif, SIGTERM shutdown bersih; 10 test baru (241 total) |
| 2026-11-08 | T-015 | Selesai | /markets (daftar watch_config + tombol inline toggle -> set_watch_enabled kebalikan, edit pesan idempoten + konfirmasi singkat) dan /results (validasi aset/timeframe domain, limit 1-20, error ramah saat kosong) lewat use case ListResults + latest_results; pesan dibatasi 3800 char; test formatter + callback toggle via feed_update (13 test baru, 254 total hijau) |
| 2026-11-08 | T-016 | Selesai | Port WatchlistRepository (add/remove/list/is_watched) + impl PostgreSQL idempoten (ON CONFLICT DO NOTHING); use case wallet_views: TopWallets (/top default 10 maks 25, tanpa market maker, tampil skor/edge/ROI/jumlah market/win rate) dan WalletDetail (statistik lengkap + status watchlist, validasi normalize_wallet → pesan ramah); use case manage_watchlist (/watch //unwatch idempoten dengan balasan singkat); handler opsional di build_router; 14 unit test (formatter/validasi/idempotensi fake repo) + 4 integration test PostgreSQL (272 total) |
| 2026-11-08 | T-017 | Selesai | Use case evaluate_alert dari hook on_saved ingest_trades: is_alertable (sampel cukup, bukan MM), skor ≥ min_score (wallet watchlist tetap terkirim prefix [WATCHLIST]), usdc ≥ min_usdc, cooldown per (user, wallet) 300 dtk, catat alerts_sent dengan delay_ms; formatter alert Indonesia (slug, wallet 0xabab…abab, skor & edge, beli/jual Up/Down, size USDC, harga, link market); manage_alerts + /alerts dengan tombol inline (skor 50/60/70/80, size $50/$100/$500, cooldown 1/5/15 mnt, on/off); AlertSettingsRepository & AlertsSentRepository PostgreSQL (trade_id via kunci idempoten); container setup_ingest merakit on_saved → evaluate_alert (hook opsional); 19 test baru (291 total); runtime: pesan "alert pipeline siap" + contoh alert terkirim ke owner (message_id 5) |
| 2026-11-08 | T-019 | Selesai | HealthMonitor (application/use_cases/health_monitor.py): cek trade terakhir market LIVE via port baru HealthActivityRepository.last_trade_at_live (impl PostgreSQL max(traded_at) join markets LIVE), siklus discover/result terakhir via record_cycle dari scheduler (sukses = aktivitas, gagal tidak); ambang STALE_INGEST_S=120/STALE_DISCOVER_S=180; rate limit 1 notifikasi per jenis per 10 mnt (HEALTH_RATE_LIMIT_S=600); notifikasi pemulihan saat komponen aktif kembali (via check atau record_cycle); grace period 300 dtk saat start tanpa data; state in-memory (ADR-008); notifier gagal kirim di-log tidak melempar. Runner: _health_loop tiap HEALTH_CHECK_INTERVAL_S=30 dtk (no-op tanpa monitor); Container.setup_health merakit TelegramNotifier owner. 19 test baru (297 ke 316) hijau; gate ruff/format/mypy strict 105 file/import-linter 3 kept |
| 2026-11-08 | T-020 | Selesai | scripts/backup.sh (pg_dump -Fc ke BACKUP_DIR/<slug>-YYYYmmdd-HHMMSS.dump, koneksi APP_DB_URL atau PG*, rotasi find -mtime +14 -delete via RETENTION_DAYS, file .partial dihapus saat gagal + exit non-zero, set -euo pipefail) dan scripts/restore.sh (pg_restore ke target eksplisit, konfirmasi interaktif "RESTORE" atau --yes, tolak PROD_DATABASE default polywatch_db tanpa --force-prod); tanpa path/kredensial hardcoded; README bagian backup/restore + contoh cron. Dibuktikan nyata: backup polywatch_db ke polywatch-20261108-172513.dump (27K) lalu restore ke polywatch_restore_test: 12 tabel, markets 0=0, trades 0=0; rotasi menghapus file berumur 20 hari; bash -n bersih (shellcheck tidak tersedia di host); 2 bug restore tertangkap dan diperbaiki saat verifikasi nyata (psql salah target saat PGDATABASE ikut ter-set; regex nama DB salah parse URL sehingga proteksi prod tidak jalan) |
| 2026-11-08 | T-023 | Selesai || 2026-11-08 | T-024 | Selesai | Discovery market dirombak jadi berbasis slug per jendela waktu (ADR-010). Akar masalah: list-endpoint `closed=false` mengembalikan market jangka panjang non-updown (0 slug updown di 100 item pertama) dan `end_date_max` mengembalikan market basi Des 2025 yang tak pernah ditutup (54 item updown, semua volume 0) — orkestrasi lama melewatkan market live. Pendekatan baru: untuk tiap (aset, timeframe) hitung window start = floor(now/durasi)*durasi lalu `GET /markets?slug=<slug>` untuk jendela berjalan + 2 ke depan (closed=false) dan 3 ke belakang (closed=true, untuk record_result — market live hilang dari pencarian slug setelah end). Aset dari tabel assets (port AssetSource baru) ∪ default Settings MARKET_ASSETS; timeframe domain; pembentukan slug (`window_start`/`build_slug`) di application layer, adapter Gamma hanya eksekusi HTTP + throttle ≤5 req/s. Filter anti-stale: slug wajib cocok pola updown DAN endDate ±6 jam dari sekarang (log `skipped_stale`) — jalur cadangan list-endpoint tetap ada sebagai supplement dan wajib lolos filter yang sama. Bukti runtime nyata (scripts/spike/t024_discover_live.py, DB polywatch_test): 90 market tersimpan = 16 LIVE (8 aset × 5m/15m: bnb,btc,doge,eth,hype,sol,xrp,zec — persis sebanding 16 yang diukur manual) + 32 UPCOMING + 42 CLOSED, 0 market basi; run kedua 80 market (16 live) konsisten. Test: unit window_start (batas tepat 5m/15m, offset depan/belakang), build_slug (validasi balik MarketSlug.parse), filter stale (endDate 11 bulan lalu ditolak), jendela belakang closed=true; integration respx fixture nyata (live tersimpan, basi ditolak, upsert 2× tanpa duplikat, param closed benar); e2e core flow disesuaikan. 317 → 339 test; gate ruff/format/mypy strict 107 file/import-linter 3 kept |
 Hotfix konkurensi `AsyncSession`. Bug produksi: `/markets` & `/status` gagal `InvalidRequestError: This session is provisioning a new connection; concurrent operations are not permitted` karena `main/container.py` merakit use case bot dengan SATU `AsyncSession` yang dipakai bersama lintas update Telegram (aiogram memproses update konkuren), scheduler, dan ingest — satu AsyncSession tidak boleh dipakai bersamaan. Perbaikan (ADR-009): akses DB per-task/per-update — `async_scoped_session(async_sessionmaker, scopefunc=asyncio.current_task)` di container untuk scheduler/ingest/health (tiap siklus `commit` + `remove()` di `finally`), plus provider use case per-update untuk handler bot (`_bot_providers`): tiap update merakit use case dengan session sendiri, `SessionReleaseMiddleware` commit + remove session di `finally` tiap update (unit-of-work per update, jalur error tetap melepas session — tidak ada kebocoran koneksi; gagal commit/release di-log `session_commit_error`/`session_release_error`). Handler opsional (/top /wallet /watch /unwatch /alerts + callback alert) juga per-update. Use case tetap tak tahu SQLAlchemy; container tetap composition root; layer tidak berubah. Test regresi `tests/e2e/test_concurrent_updates.py`: 6 update (2×/markets, /status, /top, /alerts, callback toggle) via `asyncio.gather` atas `Dispatcher.feed_update` dengan DB nyata polywatch_test — reproduksi bug lama terkonfirmasi (RED sebelum fix), semua sukses + tulisan toggle ter-commit setelah fix. 316 → 317 test; gate ruff/format/mypy strict 106 file/import-linter 3 kept; runtime 20 dtk: getMe + polling aktif tanpa error session konkurensi |
| 2026-11-08 | T-025 | Selesai | Hotfix kontrak siklus scheduler. Akar masalah: `main/container.py` menyimpan fungsi coroutine (`run_discover` dll) sedangkan `main/scheduler.py` memanggil `.execute()` via protocol — setiap siklus gagal AttributeError yang ditangkap `_run_cycle` dan hanya di-log (`scheduler_cycle_error`), sehingga discover/record_result/recompute tidak pernah jalan, tabel `markets` tetap 0, dan health monitor terus melaporkan komponen basi. Test lama tidak menangkap karena memakai fake ber-`.execute()`; field container bertipe `Any | None` membuat mypy buta. Perbaikan (ADR-011): kontrak tunggal = callable coroutine — `DiscoverCycle = Callable[[], Awaitable[object]]`, `ResultCycle = Callable[[], Awaitable[Sequence[object]]]`, `RecomputeCycle = Callable[[int], Awaitable[object]]` (menggantikan 3 Protocol ber-`.execute()`); scheduler memanggil callable langsung (`_run_cycle("discover", discover)` bukan `discover.execute`); field container `discover_cycle`/`record_result_cycle`/`recompute_cycle` diberi tipe eksplisit + `health_monitor: HealthMonitor | None` (TYPE_CHECKING) sehingga mypy strict menangkap ketidakcocokan kontrak; `ingest` hook dan `HealthReporter` sudah sepakat kontrak callable, tidak berubah. Test kontrak baru `tests/integration/test_scheduler_contract.py`: objek scheduler-ready diambil dari Container nyata (`setup_scheduler`) — `inspect.iscoroutinefunction` ketiga siklus + loop scheduler interval milidetik dengan Gamma di-mock respx & DB nyata polywatch_test berjalan ≥1 siklus tanpa `scheduler_cycle_error` dan market tersimpan (regresi: sebelum fix `markets` tetap 0). Test unit scheduler dipindah ke fake callable sesuai kontrak. Bukti runtime nyata (timeout 260 dtk `uv run python -u -m polywatch.main.cli`, DB lokal polywatch_db): log `discover_markets selesai` new_markets=18 total_markets=88; `select count(*) from markets` = 104 (LIVE 23, RESOLVED 48; awal run 0); 0 `scheduler_cycle_error`; 17 `market_result_saved` + 17 `recompute_scores_done`. Catatan temuan baru (di luar scope, belum ditangani): run sebelumnya sempat menangkap 2 `DeadlockDetectedError` PostgreSQL antar-loop discover vs record_result saat upsert baris market yang sama bersamaan — session per-task (T-023) tidak mencegah deadlock level baris; siklus berikutnya otomatis sukses, layak jadi backlog. 339 → 342 test; gate ruff/format/mypy strict 108 file/import-linter 3 kept |
| 2026-11-08 | T-026 | Selesai | Hotfix semantik watch + ingest kontinyu. Akar masalah #1: `SqlAlchemyLiveTokenQuery` INNER JOIN `watch_config enabled=true` sementara tabel `watch_config` kosong (owner belum menyentuh /markets) → 0 token live → ingest selamanya dilewati (`tidak ada token live; siklus dilewati`, trades tetap 0 padahal 23 market LIVE). Perbaikan (ADR-012): baris watch_config hanya menyimpan override — pasangan tanpa baris = enabled; LiveTokenQuery pakai LEFT JOIN + `IS DISTINCT FROM FALSE`; `is_enabled` default True; method baru `effective_watch_all` (aset terdaftar x timeframe) dipakai `list_watch`/`enabled_assets` sehingga /markets konsisten menampilkan semua kombinasi; tanpa perubahan skema. Akar masalah #2: ingest lama dipolling per-siklus dan commit hanya di akhir siklus — stream kontinyu tidak pernah "berakhir" sehingga trade tidak pernah ter-commit; plus query token live memakai scoped session task ingest dan `release_session` menutup session yang sedang dipakai repo trade. Perbaikan (ADR-013): `IngestTrades.run(stop_event)` loop kontinyu seumur proses — refresh token tiap 30 dtk lewat port `LiveTokenSource` (session sekali-pakai dari factory), stream WS restart dengan token terbaru saat server tutup (close 1000 per jendela), **commit per batch** via `on_batch_commit`; runner menjalankan hook ingest sebagai task tunggal; `OrderbookWsClient` dapat `resubscribe()` + penghentian rapi (ping_task cancel menelan CancelledError, recv timeout mengecek stop). Bonus fix: `setup_health` NameError (HealthMonitor hanya TYPE_CHECKING import, sisa T-025) → import lokal. Akar masalah #3: deadlock PostgreSQL discover vs record_result (upsert baris market sama) → `_execute_with_deadlock_retry` pada upsert/save_result/update_status MarketRepository: 3 percobaan, backoff 0.2s*2^n, log terstruktur `db_deadlock_retry`/`db_deadlock_exhausted`; deteksi via SQLSTATE 40P01 (asyncpg dibungkus DBAPIError). Test baru: default-enabled + override (unit manage_settings + integration watch_config dan live_token_query: tabel kosong → semua market live; override false → hanya pasangan itu dikecualikan), loop kontinyu (3 siklus stream, dedupe lintas siklus, gagal query token tetap hidup, commit per batch), retry deadlock (sukses setelah retry, error non-deadlock diteruskan, habis retry diteruskan + log). Bukti runtime nyata: (a) log `mulai ingest ws` tokens=44/68 > 0; (b) `select count(*) from trades` = 3162 (> 0; run debug 110 dtk menangkap 3159 trade dari 451 event pada jam ramai); (c) contoh trade: wallet `0x2397e5b6...` outcome DOWN action BUY/SELL price 0.999 size 12.5 usdc 12.4875 market 1031 tx `0x7be5426e...`; (d) alerts_sent=0 — wajar: wallet_stats masih 0 karena recompute_scores baru jalan setelah market resolve (awal siklus hidup). 342 → 356 test; gate ruff/format/mypy strict 110 file/import-linter 3 kept |
| 2026-11-08 | T-028 | Selesai | Hotfix siklus hidup market & ingest berbasis jendela waktu (ADR-015). Akar masalah (diukur di DB & log, pasca-T-027): aplikasi jalan bersih tapi `max(traded_at)` berhenti — seluruh 38 market status LIVE sudah lewat `end_at` (query `status='LIVE' and end_at > now()` = 0 baris), `LiveTokenQuery` memilih dari status LIVE sehingga WS hanya subscribe market selesai → 0 event; plus suplemen list-endpoint Gamma membanjiri journald ~8.000 WARNING `payload rusak` per siklus discover. Perbaikan: (1) use case baru `SweepMarketStatus` — batch idempoten yang menyelaraskan status dengan jendela waktu memakai aturan domain `status_at`: UPCOMING/LIVE dengan `end_at < now` → CLOSED (via `transition_status`; inilah yang membuat `record_result` menemukan market untuk di-resolve), dan UPCOMING yang jendelanya berjalan → LIVE (siklus discover penuh 8 aset bisa >5 menit — tanpa ini market baru tak pernah LIVE; ditemukan saat run verifikasi kedua); dipanggil container di awal siklus result. Port `MarketRepository.list_stale_open` + DTO `StaleMarket`. (2) `SqlAlchemyLiveTokenQuery` berbasis jendela waktu: `start_at - 5 dtk <= now < end_at` dan status ≠ RESOLVED (clock di-inject), tidak lagi memilih dari status LIVE; test baru membuktikan market LIVE basi TIDAK diambil dan market aktif walau statusnya UPCOMING tetap diambil. (3) Suplemen list-endpoint diputus dari jalur normal — container tidak lagi memasang `supplement_source` (param kini opt-in eksplisit), log `payload rusak` diturunkan ke DEBUG dengan rate-limit 60 dtk (`_log_broken_payload`). Bukti run nyata (satu instance, `timeout 420`, 15:24–15:31Z, DB polywatch_db): sweep `closed=7 started=7` + `closed=8 started=8`; `mulai ingest ws tokens=26 markets=13`; trades 5.341 → 8.243 (+2.902, semua `traded_at > 15:24:07Z`; contoh tx `0x72c8e8d6...` wallet `0x440bfa4f...` market 6407); market_results 549 → 580 (+31); 0 WARNING payload rusak; 16 market aktif ter-ingest saat jendela berjalan. Catatan debt: siklus discover penuh (~4-5 mnt utk 10 aset) > interval scheduler 30 dtk membuat health monitor sempat menandai discover basi antar-siklus — pre-existing, kandidat backlog (paralelisasi per-aset). 376 → 393 test; gate ruff/format/mypy strict 115 file/import-linter 3 kept |
