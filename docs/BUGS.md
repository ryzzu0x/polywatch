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
| BUG-011 | Siklus discover penuh 4–5 menit > interval scheduler 30 dtk | major | Dipantau | — |

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
- **Rencana:** paralelisasi permintaan per aset (batas ≤5 req/s), atau naikkan interval discover ke 120–180 dtk sambil mempersingkat jumlah jendela ke depan.
- **Status:** Dipantau (technical debt, bukan blocker MVP).

---

## Pelajaran yang diambil

1. **Error yang ditelan `try/except` adalah bug tersembunyi.** Tiga bug (BUG-003, BUG-005, BUG-008) sempat "hijau" karena exception hanya masuk log sementara proses tetap hidup. Setiap `except` harus punya jalur bukti (test atau alarm).
2. **`Any` di composition root menyembunyikan ketidakcocokan kontrak.** Tipe eksplisit + test kontrak lewat container nyata menutup celah ini.
3. **Test dengan fake bisa lolos meski produksi rusak.** Test kontrak harus memakai wadah nyata (Container), bukan hanya objek palsu.
4. **Metrik kesehatan harus berbasis heartbeat komponen**, bukan turunan data bisnis yang wajar kosong (pasar sepi).
5. **Satu instance, satu lock.** Dua proses menghasilkan alert ganda dan konflik polling yang membingungkan saat diagnosa.
