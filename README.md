# polywatch

Arsip hasil market **Up/Down** Polymarket (5m & 15m) + skor wallet + alert copy-trade ke Telegram.

Tujuan: memberi tahu lebih cepat daripada mata manusia, wallet mana yang benar-benar punya edge —
bukan sekadar menang banyak — lalu (v2) mengeksekusi copy-trade dari wallet EVM yang dibuat otomatis
untuk setiap pengguna.

## Status

`v0.1.0` — fondasi. Domain, port, config, logging, dan quality gate sudah jalan.
Bot Telegram, ingest data, dan skor penuh menyusul sesuai `docs/TASKS.md`.

Roadmap singkat:

- **MVP** — arsip hasil market + ingest trade + skor wallet + alert Telegram (alert saja, tanpa eksekusi).
- **v1** — paper copy-trade untuk membuktikan edge bertahan setelah delay.
- **v2** — wallet EVM per pengguna, copy-trade otomatis, batas modal + kill switch + ekspor private key.

## Arsitektur

Clean Architecture, satu proses (modular monolith):

```
presentation  →  application  →  domain
infrastructure →  application (mengimplementasikan port)
main/         = composition root
```

Aturan dependensi dipaksa `import-linter`, jadi pelanggaran layer membuat CI merah.

```
src/polywatch/
├── domain/          market, trade, position, scoring
├── application/     ports/ (kontrak), use_cases/, dto/
├── infrastructure/  polymarket/, prices/, db/, clock
├── presentation/    bot/
└── main/            config, logging, container, cli
```

Detail lengkap: `docs/ARCHITECTURE.md`.

## Prasyarat

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (dibutuhkan mulai T-004)

## Setup lokal

```bash
git clone git@github.com:ryzzu0x/polywatch.git
cd polywatch
uv sync --group dev
cp .env.example .env    # lalu isi nilainya
```

## Menjalankan

```bash
uv run polywatch --check     # validasi konfigurasi .env
uv run polywatch             # start aplikasi
```

Tanpa konfigurasi lengkap, aplikasi berhenti dengan pesan field mana yang bermasalah — tidak ada
nilai default yang membahayakan.

## Backfill skor wallet (T-032)

`polywatch recompute-all` menghitung ulang skor SEMUA wallet kandidat (union baris `wallet_stats`
dan wallet dengan trade di market RESOLVED) — dipakai setelah ambang `MIN_SAMPLE_MARKETS` turun
(ADR-018) atau untuk memperbaiki baris lama. Subcommand ini satu-kali: tidak menjalankan bot,
scheduler, atau ingest, dan tidak mengambil instance lock, jadi aman dijalankan saat service hidup.

```bash
uv run polywatch recompute-all --dry-run   # hitung tanpa menulis (preview)
uv run polywatch recompute-all             # backfill nyata + commit
uv run polywatch recompute-all --limit 50  # hanya 50 wallet pertama (urut wallet)
```

Output ringkasan: jumlah wallet diproses, wallet dengan skor > 0, skor maksimum, dan durasi.
Idempoten — menjalankan ulang dari sumber yang sama menghasilkan nilai yang sama.

## Quality gate

Wajib hijau sebelum merge ke `main`:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest
```

## Test

```bash
uv run pytest              # semua test
uv run pytest tests/unit   # hanya unit
```

Piramida: banyak unit (domain, use case), cukup integration (repo + PostgreSQL, respons API via `respx`),
sedikit e2e (market → trade → alert → resolve → skor).

## Struktur repository

```
src/            kode aplikasi (Clean Architecture)
tests/          unit, integration, e2e
scripts/        backup.sh, restore.sh (T-020), deploy (T-021)
docs/           salinan dokumen planning — sumber kebenaran ada di workspace planning
.github/        CI
```

## Backup & restore database (T-020)

Kedua skrip membaca semua nilai dari environment — tidak ada path server
atau kredensial yang ditulis di dalam skrip.

**Backup** (`scripts/backup.sh`) — `pg_dump` format custom (`-Fc`) ke
`${BACKUP_DIR}/<slug>-YYYYmmdd-HHMMSS.dump`, rotasi otomatis menyimpan
14 hari (`RETENTION_DAYS`), dump gagal di tengah jalan meninggalkan file
parsial dihapus dan exit non-zero:

```bash
BACKUP_DIR=/path/backup APP_DB_URL="postgresql://user:pass@host:5432/polywatch_db" \
  ./scripts/backup.sh
# alternatif tanpa URL: export PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
```

**Restore** (`scripts/restore.sh`) — restore dump terbaru (atau
`RESTORE_FILE`) ke database target. Wajib konfirmasi interaktif (ketik
`RESTORE`) kecuali `--yes`; menolak restore ke database produksi
(`PROD_DATABASE`, default `polywatch_db`) tanpa `--force-prod`:

```bash
BACKUP_DIR=/path/backup APP_DB_URL="postgresql://user:pass@host:5432/polywatch_test" \
  ./scripts/restore.sh --yes
```

**Jadwal cron contoh** (backup harian 03:00; path sesuaikan environment,
jangan hardcode di skrip):

```cron
0 3 * * * cd /path/ke/app && BACKUP_DIR=/path/backup BACKUP_SLUG=polywatch \
  APP_DB_URL="$POLYWATCH_DB_URL" ./scripts/backup.sh >> /path/backup/backup.log 2>&1
```

## Deploy

Ringkas: push → `git pull` di server → `uv sync` → `alembic upgrade head` → restart service `polywatch`.
Kode tidak pernah diedit langsung di server. Prosedur detail menyusul di T-021.

## Keamanan

- Secret hanya lewat environment variable; `.env` di-ignore dan tidak pernah di-commit.
- Bot MVP hanya melayani `OWNER_CHAT_ID`.
- v2: private key pengguna dienkripsi AES-GCM dengan `WALLET_MASTER_KEY` di luar database,
  tidak pernah dicetak ke log, bisa diekspor pemiliknya sendiri lewat perintah eksplisit.

## Lisensi

Proprietary — semua hak dipegang pemilik project.
