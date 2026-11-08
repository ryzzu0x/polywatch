#!/usr/bin/env bash
# Backup database polywatch via pg_dump (format custom -Fc) + rotasi 14 hari.
#
# Semua nilai dari environment — TIDAK ada path/kredensial hardcoded:
#   BACKUP_DIR      (wajib) direktori tujuan dump
#   BACKUP_SLUG     (opsional, default "polywatch") prefiks nama file
#   APP_DB_URL      (opsional) URL lengkap postgresql://user:pass@host:port/db
#   PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE  (alternatif tanpa APP_DB_URL)
#   RETENTION_DAYS  (opsional, default 14) rotasi backup lama
#
# Contoh: BACKUP_DIR=/var/backups/polywatch APP_DB_URL=postgresql://... ./backup.sh
set -euo pipefail

readonly Program="backup.sh"

die() {
  printf '%s: %s\n' "$Program" "$*" >&2
  exit 1
}

log() {
  printf '%s: %s\n' "$Program" "$*"
}

# --- Validasi env ------------------------------------------------------------
: "${BACKUP_DIR:?env BACKUP_DIR wajib diisi (direktori tujuan backup)}"
readonly BACKUP_DIR
SLUG="${BACKUP_SLUG:-polywatch}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

# --- Sumber koneksi: APP_DB_URL atau PG* --------------------------------------
DB_ARGS=()
if [ -n "${APP_DB_URL:-}" ]; then
  DB_ARGS=(--dbname "$APP_DB_URL")
elif [ -n "${PGDATABASE:-}" ]; then
  DB_ARGS=(--dbname "$PGDATABASE")
else
  die "koneksi DB tidak diketahui: set APP_DB_URL atau PGDATABASE (lihat PGHOST/PGUSER/PGPASSWORD)"
fi

command -v pg_dump >/dev/null 2>&1 || die "pg_dump tidak ditemukan di PATH"

# --- Pastikan direktori tujuan ada --------------------------------------------
mkdir -p -- "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
FILENAME="${SLUG}-${TIMESTAMP}.dump"
TARGET="${BACKUP_DIR}/${FILENAME}"
# File sementara: bila gagal di tengah, parsial dihapus (jangan tinggalkan
# dump setengah yang terlihat valid).
PARTIAL="${TARGET}.partial"

log "mulai pg_dump -> ${TARGET}"

# --- Dump + hapus parsial bila gagal ------------------------------------------
if ! pg_dump -Fc "${DB_ARGS[@]}" -f "$PARTIAL"; then
  rm -f -- "$PARTIAL"
  die "pg_dump gagal; file parsial dihapus: ${PARTIAL}"
fi
mv -- "$PARTIAL" "$TARGET"

SIZE="$(du -h -- "$TARGET" | cut -f1)"
log "backup selesai: ${TARGET} (${SIZE})"

# --- Rotasi: hapus backup lebih tua dari RETENTION_DAYS ----------------------
ROTATED="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${SLUG}-*.dump" -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"
if [ "$ROTATED" -gt 0 ]; then
  log "rotasi: ${ROTATED} backup lebih tua dari ${RETENTION_DAYS} hari dihapus"
fi
