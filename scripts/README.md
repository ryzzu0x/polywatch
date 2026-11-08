# scripts/

Skrip operasional. Belum diisi pada fondasi (v0.1.0); jadwalnya ada di `docs/TASKS.md`:

| Skrip | Task | Isi |
|---|---|---|
| `deploy.sh` | T-021 | pull → `uv sync` → `alembic upgrade head` → restart service |
| `backup.sh` | T-020 | `pg_dump` harian, retensi 14 hari |
| `restore.sh` | T-020 | restore dump ke database uji |

Aturan: setiap skrip menerima path lewat environment variable (tidak ada path server yang
di-hardcode di repo), berhenti pada error pertama, dan aman dijalankan ulang.
