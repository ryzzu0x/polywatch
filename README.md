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
scripts/        deploy, backup, restore (dibuat di T-020/T-021)
docs/           salinan dokumen planning — sumber kebenaran ada di workspace planning
.github/        CI
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
