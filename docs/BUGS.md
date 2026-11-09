# BUGS — polywatch

> Daftar bug yang ditemukan selama build MVP, lengkap dengan bukti, akar masalah, perbaikan, dan status.
> Format ID: `BUG-XXX`. Status: **Tertutup** · **Terbuka** · **Dipantau**.

| ID | Ringkasan | tingkat | Status | Commit |
|---|---|---|---|---|
| BUG-001 | Satu `AsyncSession` dipakai bersama lintas task | critical | Tertutup | `eab02e3` |
| BUG-002 | Discovery mengambil market basi / melewatkan yang live | critical | Tertutup | `bb7ffe0` |
| BUG-003 | Kontrak siklus scheduler tidak cocok (`.execute()` vs callable) | critical | Tertutup | `9c48495` |
| BUG-004 | `watch_config` kosong dianggap "semua mati" | critical | Tertutup | `c020c34` |
| BUG-005 | Ingest putus tiap siklus & tidak pernah commit | critical | Tertutup | `7acf34b` |
| BUG-006 | Deadlock PostgreSQL antar-loop | major | Tertutup | `c020c34` |
| BUG-007 | Alert kesehatan palsu beruntun + instance ganda | major | Tertutup | `8f45fa4` |
| BUG-008 | Status market tidak pernah maju (LIVE basi) | critical | Tertutup | `27b62ac` |
| BUG-009 | Banjir WARNING `payload rusak` dari suplemen Gamma | major | Tertutup | `27b62ac` |
| BUG-010 | Cacat dokumen: CHANGELOG ganda, T-018/T-027 tidak tercatat | minor | Tertutup | `cc43404` + docs |
| BUG-011 | Siklus discover penuh 4–5 menit > interval scheduler 30 dtk | major | Tertutup | `0901f0f` + `6a802b1` |
| BUG-012 | Kegagalan sumber harga satu market menjatuhkan siklus record_result | major | Tertutup | `d282664` |
| BUG-013 | Stream WS hidup tapi bisu + token tidak pernah di-refresh saat stream hidup | critical | Tertutup | `d282664` + `abe9373` |
| BUG-014 | Notifikasi kesehatan/pemulihan bisa spam | major | Tertutup | `d282664` |
| BUG-015 | Session ingest keracun: PendingRollbackError berantai setelah satu kegagalan DB | critical | Tertutup | T-031 |

---

## BUG-001 — Satu `AsyncSession` dipakai bersama lintas task

- **Gejala:** `/markets` dan `/status` gagal di Telegram; `/start` (tanpa DB) tetap jalan sehingga terasa "sebagian jalan".
- **Bukti:**
  ```
  sqlalchemy.exc.InvalidRequestError: This session is provisioning a new connection;
  concurrent operations are not permitted
  ```
- **Akar masalah:** `main/container.py` merakit use case dengan satu `AsyncSession` saat startup. aiogram memproses tiap update sebagai task asyncio terpisah, jadi satu session dipakai bersamaan.
- **Perbaikan:** akses DB per-task/per-update — `async_scoped_session` untuk scheduler/ingest, provider use case per-update untuk handler, `SessionReleaseMiddleware` commit + release di `finally` (ADR-009).
- **Pencegahan:** test regresi 6 update konkuren (`/markets`×2, `/status`, `/top`, `/alerts`, callback toggle) dengan DB nyata.
- **Status:** Tertutup — `eab02e3`.

## BUG-002 — Discovery mengambil market basi / melewatkan yang live

- **Gejala:** DB kosong padahal ada 16 market Up/Down live.
- **Bukti:**
  - `GET /markets?closed=false&order=endDate&ascending=true&end_date_min=<now-1h>` → 100 item, **0 slug updown** (isi: market jangka panjang).
  - `end_date_max=<now+60m>` → 54 item updown, **semuanya sisa Des 2025**, volume 0.
  - `GET /markets?slug=btc-updown-5m-<window>` → market live, `closed=false`, volume 19,6.
- **Akar masalah:** orkestrasi bergantung pada urutan list-endpoint; market updown tidak ada di halaman awal, sementara market basi menempati urutan atas.
- **Perbaikan:** discovery deterministik dari slug per jendela waktu (`floor(now/durasi)*durasi`), jendela berjalan + 2 ke depan + 3 ke belakang (`closed=true`), filter anti-stale `endDate ±6 jam` (ADR-010).
- **Bukti perbaikan:** run nyata menyimpan 90 market = 16 LIVE + 32 UPCOMING + 42 CLOSED, 0 market basi.
- **Status:** Tertutup — `bb7ffe0`.

## BUG-003 — Kontrak siklus scheduler tidak cocok

- **Gejala:** `markets` tetap 0 walau log `scheduler start` terbit; health monitor terus melaporkan "komponen basi".
- **Bukti:** `GAGAL: AttributeError 'function' object has no attribute 'execute'`
- **Akar masalah:** container menyimpan **fungsi** (`run_discover`), scheduler memanggil `.execute()` lewat Protocol. Error ditangkap `_run_cycle` lalu hanya di-log → semua siklus gagal diam-diam tiap 30 detik. Lolos review karena test memakai fake ber-`.execute()` dan field container bertipe `Any | None`.
- **Perbaikan:** satu kontrak (callable coroutine, ADR-011), field container bertipe eksplisit, test kontrak memakai Container nyata (bukan fake).
- **Bukti perbaikan:** `discover_markets selesai new_markets=18`, `markets` = 104, 0 `scheduler_cycle_error`.
- **Status:** Tertutup — `9c48495`.

## BUG-004 — `watch_config` kosong dianggap "semua mati"

- **Gejala:** 23 market LIVE tapi `trades` = 0.
- **Bukti:** log `"tidak ada token live; siklus dilewati"` berulang; `select count(*) from watch_config` = 0.
- **Akar masalah:** query token memakai INNER JOIN `watch_config enabled=true`. Owner belum pernah membuka `/markets` → nol baris → nol token → ingest mustahil berjalan.
- **Perbaikan:** semantik default-enabled — tabel kosong berarti semua aset × timeframe aktif; baris `watch_config` hanya menyimpan override (ADR-012). `/markets` konsisten lewat `effective_watch_all`.
- **Status:** Tertutup — `c020c34`.

## BUG-005 — Ingest putus tiap siklus & tidak pernah commit

- **Gejala:** stream WS terbuka lalu tertutup; trade yang terdeteksi tidak tersimpan.
- **Akar masalah:** `run_once` dipolling tiap siklus dan commit hanya di akhir siklus, padahal stream WS kontinu tidak pernah "berakhir" → commit tidak pernah tercapai. Query token memakai session yang sudah di-release.
- **Perbaikan:** `IngestTrades.run(stop_event)` hidup seumur proses, refresh token tiap 30 dtk, restart otomatis saat server menutup koneksi (`close 1000 "all subscribed assets resolved"`), **commit per batch** (ADR-013).
- **Bukti perbaikan:** `mulai ingest ws tokens=44`, `trades` 0 → 3.986 dalam satu run.
- **Status:** Tertutup — `7acf34b`.

## BUG-006 — Deadlock PostgreSQL antar-loop

- **Gejala:** `DeadlockDetectedError` saat `discover_loop` dan `result_loop` meng-upsert baris market yang sama.
- **Perbaikan:** `_execute_with_deadlock_retry` pada `upsert`/`save_result`/`update_status` — 3 percobaan, backoff 0,2 s × 2ⁿ, deteksi SQLSTATE 40P01, log `db_deadlock_retry` / `db_deadlock_exhausted`.
- **Bukti perbaikan:** log run nyata `db_deadlock_retry attempt 1` → siklus lanjut sukses.
- **Status:** Tertutup — `c020c34`.

## BUG-007 — Alert kesehatan palsu beruntun + instance ganda

- **Gejala:** pesan berulang *"siklus result tidak berhasil lebih dari 3 menit (aktivitas terakhir: belum ada)"* dan versi ingest-nya, dua kali dalam satu menit.
- **Tiga akar masalah:**
  1. **Dua instance berjalan bersamaan** (proses tes tidak dimatikan). Bukti: `TelegramConflictError: terminated by other getUpdates request (tryings=391)`. Dua proses = dua HealthMonitor = dua alert.
  2. **Urutan perakitan:** `setup_scheduler()` sebelum `setup_health()` → `monitor=None` → `record_cycle` tak pernah dipanggil → status selalu "belum ada".
  3. **Metrik salah ukur:** kesegaran dinilai dari trade terakhir di market LIVE; jendela 5m baru mulai / pasar sepi → `None` → alarm. Ambang 120 dtk terlalu ketat.
- **Perbaikan:** single-instance lock `fcntl.flock` (instance kedua exit 3 tanpa menyentuh Telegram/DB), urutan `setup_health` sebelum scheduler, heartbeat ingest (ambang 600 dtk, alarm hanya bila ada market LIVE), state notifikasi persisten antar-restart (ADR-014), log `httpx`/`httpcore` diredam.
- **Bukti perbaikan:** run 6,4 menit tanpa satu pun `health_stale`; percobaan instance kedua → `another_instance_running`.
- **Status:** Tertutup — `8f45fa4`.

## BUG-008 — Status market tidak pernah maju

- **Gejala:** bot jalan bersih tapi `trades` beku (max `traded_at` 14:29Z, bot start 14:40).
- **Bukti DB:**
  ```
  status    jumlah   end_at sudah lewat
  LIVE        38          38
  UPCOMING    55          23
  RESOLVED   455         455
  ```
  `status='LIVE' AND end_at > now()` → 0 baris.
- **Bukti silang:** market BTC 5m punya 3 trade aktif di Data API pada detik yang sama, tapi DB tidak menerima satu pun.
- **Akar masalah:** tidak ada mekanisme memindahkan `LIVE → CLOSED` saat `end_at` lewat; `LiveTokenQuery` memilih token dari `status='LIVE'` sehingga WS di-subscribe ke market yang sudah selesai.
- **Perbaikan:** `SweepMarketStatus` dua arah (UPCOMING/LIVE lewat waktu → CLOSED; UPCOMING yang jendelanya berjalan → LIVE) dijalankan tiap siklus result (ADR-015); `LiveTokenQuery` berbasis jendela waktu `start_at ≤ now < end_at` + status ≠ RESOLVED.
- **Bukti perbaikan:** `market_sweep_done closed=8 started=8`, `trades` 5.341 → 8.243 (+2.902), `market_results` 549 → 580.
- **Status:** Tertutup — `27b62ac`.

## BUG-009 — Banjir WARNING dari suplemen Gamma

- **Gejala:** ~8.000 WARNING per siklus discover.
- **Bukti:** `market Gamma di-skip: payload rusak` dengan `outcomes bukan [Up, Down] (slug='wsl2-wol-nfo-...')`, offset 1900.
- **Akar masalah:** list-endpoint tetap dipanggil sebagai "suplemen" padahal slug-per-jendela sudah jadi sumber kebenaran; isinya market non-updown dan paginasinya sampai offset 1900.
- **Perbaikan:** suplemen diputus dari jalur normal (kini opt-in eksplisit), log "payload rusak" turun ke DEBUG + rate limit 60 dtk.
- **Bukti perbaikan:** log run 167 baris, **0** WARNING payload rusak.
- **Status:** Tertutup — `27b62ac`.

## BUG-010 — Cacat dokumen

- `CHANGELOG.md` punya **3 bagian `[Unreleased]`** dengan blok T-014/T-015 dan T-016/T-017 terduplikasi.
- **T-018** tidak dicentang walau kodenya sudah masuk.
- **T-027** tidak pernah tercatat di `TASKS.md` (hanya disebut di baris log T-028), dan baris log T-023 tergabung dengan T-024.
- **Perbaikan:** CHANGELOG dikonsolidasi jadi satu bagian urut per task; checkbox T-018 dibetulkan; task + log T-027 ditambahkan; baris log T-023/T-024 dipisah.
- **Status:** Tertutup — `cc43404` + commit docs lanjutan.

## BUG-011 — Siklus discover lebih lambat dari intervalnya

- **Gejala:** satu siklus discover penuh (8–10 aset × 2 timeframe × beberapa jendela) memakan ~4–5 menit, sedangkan `DISCOVER_INTERVAL_S` = 30 detik. Efek samping: market baru sempat tersimpan `UPCOMING` dan jendelanya sudah berjalan sebelum siklus berikutnya.
- **Dampak saat ini:** tidak mematikan sistem — `SweepMarketStatus` dua arah menambal transisi status, dan ingest memakai jendela waktu. Namun market bisa terlambat masuk pantauan sampai ~1 jendela.
- **Akar masalah tambahan yang ditemukan saat perbaikan:** throttle Gamma memakai `time.sleep` (blocking) di dalam event loop, sehingga `asyncio.gather` tetap berjalan serial; throttle juga tidak dipanggil di jalur fetch yang sebenarnya (dead code). Selain itu `record_result` mem-poll resolusi semua market dalam satu siklus.
- **Perbaikan (ADR-016):** paralelisasi Gamma dengan `asyncio.Semaphore(GAMMA_MAX_CONCURRENCY=5)`, throttle diganti `asyncio.sleep` dan dipasang di jalur fetch nyata, `RESULT_BATCH_SIZE=10` market per siklus (sisanya `deferred`), sweep dibatch 50, jendela discovery dibatasi, cache negatif aset mati (TTL 10 mnt), ambang kesehatan adaptif `max(3 × durasi siklus terukur, floor)`, dan log `duration_ms` tiap siklus.
- **Bukti perbaikan (run verifikasi saya, satu instance, 7 menit, 16:46–16:53Z):**
  ```
  discover: n=6  median 16.804 ms  max 18.123 ms
  result:   n=7  median  5.770 ms  max  9.461 ms
  recompute: n=30 median      6 ms  max  3.039 ms
  health_stale = 0 · ERROR = 0
  trades 19.215 → 29.057 (+9.842), 5.359 di antaranya 5 menit terakhir
  market LIVE aktif = 16 · market LIVE basi = 0
  market_results 717 → 747
  ```
- **Status:** Tertutup — `0901f0f`, `09d0dfb`, `6a802b1`.

## BUG-012 — Kegagalan sumber harga satu market menjatuhkan siklus record_result

- **Gejala:** satu market dengan sumber harga error (Polymarket/Binance timeout) menjatuhkan seluruh siklus  — market lain ikut tertunda dan hasil resmi tidak tersimpan.
- **Akar masalah:** exception dari  /  tidak ditangkap per market, sehingga melempar keluar  dan membatalkan seluruh batch; sumber harga lama juga melempar (bukan ) setelah retry habis.
- **Perbaikan (T-030/ADR-017):** exception per market ditangkap (get_result dan pengambilan harga), di-log terstruktur rate-limited (), siklus lanjut; semua sumber harga gagal → hasil resmi tetap tersimpan dengan  dan open/close NULL (kolom / kini nullable + migrasi); sumber harga kembali  setelah retry habis; Binance menambah mirror publik .
- **Bukti perbaikan (soak test T-030, 2 jam):** 90× , 90× , 0 kegagalan siklus penuh.
- **Status:** Tertutup — `d282664`.

## BUG-013 — Stream WS hidup tapi bisu + token tidak di-refresh saat stream hidup

- **Gejala:** koneksi WS terbuka tetapi tidak ada event ( tidak timeout); token stream tidak berubah walau jendela market 5m/15m berganti.
- **Akar masalah:** refresh token hanya terjadi saat stream berakhir, padahal close-1000 tidak selalu datang; tanpa event, loop tunggu tidak pernah mengevaluasi umur stream; klien WS tidak memutus koneksi bisu.
- **Perbaikan (T-030/ADR-017):** refresh token juga saat stream masih hidup (interval 30 dtk) + re-subscribe paksa bila set token berubah; watchdog 180 dtk tanpa event WS padahal ada token live → paksa reconnect (); klien WS memutus koneksi bisu 60 dtk tanpa frame; T-031 memperkaya log  dengan token lama vs baru (added/removed).
- **Bukti perbaikan:** unit test (stream bisu → watchdog reconnect; token berubah → re-subscribe paksa; event rutin me-reset watchdog). **Belum ada bukti soak 25 menit** — re-subscribe dan watchdog belum teramati pada run produksi setelah fix.
- **Status:** Menunggu soak — menunggu soak 25 menit membuktikan  dengan token berubah dan  di log produksi.


**Bukti soak 26 menit (2026-11-09 04:02–04:28Z, satu instance, `/tmp/soak-t030f.log`):**
- `market_result_saved` = 240, **145 di antaranya setelah menit ke-10**
- `trades` 29.719 → 36.813 (**+7.094**), heartbeat ingest 23× (`trades_saved` 7.094, `last_event_age_s` 0,3)
- `ingest_resubscribe` 16 token ditambah / 16 dihapus pada pergantian jendela (04:05:22 dan 04:10:23); token hidup 36 → 52
- `health_stale_detected` = **0**, `scheduler_cycle_error` = **0**, `PendingRollbackError` = **0**
- durasi siklus: discover median 16,1 dtk (maks 18,7) · result median 6,6 dtk (maks 9,3)
- satu-satunya ERROR: `TelegramNetworkError` saat `timeout` memutus proses (artefak shutdown)

## BUG-014 — Notifikasi kesehatan/pemulihan bisa spam

- **Gejala:** alarm basi bisa terkirim berulang; notifikasi pemulihan bisa terkirim lebih dari sekali per episode.
- **Akar masalah:** pemulihan tidak dibedakan per episode stale; jalur tanpa market LIVE ikut me-reset penanda notifikasi; log  tidak di-rate-limit (1.137 baris / 9,5 jam pada run sebelumnya).
- **Perbaikan (T-030/ADR-017):** notifikasi pemulihan hanya sekali per episode stale; jalur no-live-market tidak me-reset ; log  di-rate-limit per komponen per 10 menit.
- **Bukti perbaikan:** unit test simulasi 2 jam (240 cek, ≤ 12 notifikasi per komponen) dan pemulihan sekali per episode. **Belum ada bukti soak 25 menit**.
- **Status:** Menunggu soak — menunggu soak 25 menit membuktikan tidak ada spam notifikasi di produksi.


**Bukti soak 26 menit (2026-11-09 04:02–04:28Z, satu instance, `/tmp/soak-t030f.log`):**
- `market_result_saved` = 240, **145 di antaranya setelah menit ke-10**
- `trades` 29.719 → 36.813 (**+7.094**), heartbeat ingest 23× (`trades_saved` 7.094, `last_event_age_s` 0,3)
- `health_rate` 16 token ditambah / 16 dihapus pada pergantian jendela (04:05:22 dan 04:10:23); token hidup 36 → 52
- `health_stale_detected` = **0**, `scheduler_cycle_error` = **0**, `PendingRollbackError` = **0**
- durasi siklus: discover median 16,1 dtk (maks 18,7) · result median 6,6 dtk (maks 9,3)
- satu-satunya ERROR: `TelegramNetworkError` saat `timeout` memutus proses (artefak shutdown)

## BUG-015 — Session ingest keracun: PendingRollbackError berantai

- **Gejala (soak test T-030, log `/tmp/polywatch-t030-soak.log`):** 14× `ingest_stream_error` berantai dengan pesan akhir:
  ```
  sqlalchemy.exc.PendingRollbackError: Can't reconnect until invalid
  transaction is rolled back.
  ... ingest_trades.py:444 _process_tx -> trade_repository.py:65
  insert_many -> session.execute
  ```
  Setelah satu kegagalan DB: semua penyimpanan trade berikutnya gagal, stream restart berulang tiap ~35 detik, dan token stream tidak pernah berubah (selalu 36) walau jendela market berganti.
- **Akar masalah:** loop ingest `run_ingest` merakit `IngestTrades` dengan SATU repository yang mengikat scoped session task ingest seumur proses. Ketika satu `insert_many` gagal, exception naik ke `run()` (di-log `ingest_stream_error`) tetapi transaksi session tidak pernah di-rollback — session berstatus invalid permanen. Siklus berikutnya memakai session yang sama → `PendingRollbackError` untuk setiap insert → tidak ada trade yang tersimpan lagi sampai proses restart. `release_session` di `finally` baru jalan saat loop keluar, terlambat.
- **Perbaikan (T-031):** dua lapis.
  1. **Session baru per batch (infrastruktur):** `BatchSessionTradeRepository` membuka session baru dari `session_factory` untuk setiap `insert_many`, commit saat sukses, rollback + close di `finally` — kegagalan satu batch tidak meninggalkan session invalid yang menular. Container memasang repo ini untuk ingest.
  2. **Isolasi kegagalan (use case):** `_insert_batch` menangkap exception penyimpanan, me-log terstruktur rate-limited `ingest_batch_error` (kegagalan pertama + tiap ke-10) dan heartbeat not-ok, lalu stream LANJUT ke tx berikutnya — satu batch gagal tidak mematikan stream.
- **Bukti perbaikan:** unit test reproduksi (insert gagal sekali → batch berikutnya tetap tersimpan, `ingest_batch_error` ter-log; stream tetap satu koneksi saat insert selalu gagal; session gagal → rollback + close, session berikutnya segar dan bisa commit) + integration DB (constraint violation → session baru tetap sukses).
- **Status:** Tertutup — T-031 (`fix(T-031)`).

---

## Pelajaran yang diambil

1. **Error yang ditelan `try/except` adalah bug tersembunyi.** Tiga bug (BUG-003, BUG-005, BUG-008) sempat "hijau" karena exception hanya masuk log sementara proses tetap hidup. Setiap `except` harus punya jalur bukti (test atau alarm).
2. **`Any` di composition root menyembunyikan ketidakcocokan kontrak.** Tipe eksplisit + test kontrak lewat container nyata menutup celah ini.
3. **Test dengan fake bisa lolos meski produksi rusak.** Test kontrak harus memakai wadah nyata (Container), bukan hanya objek palsu.
4. **Metrik kesehatan harus berbasis heartbeat komponen**, bukan turunan data bisnis yang wajar kosong (pasar sepi).
5. **Session berumur panjang adalah bom waktu.** AsyncSession yang ditahan seumur loop (BUG-015) menjadi invalid permanen setelah satu kegagalan dan meracuni semua operasi berikutnya — unit-of-work per batch (session baru + rollback/close di finally) adalah satu-satunya pola aman untuk loop kontinyu.
6. **Satu instance, satu lock.** Dua proses menghasilkan alert ganda dan konflik polling yang membingungkan saat diagnosa.
