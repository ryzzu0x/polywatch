#!/usr/bin/env bash
# Restore dump polywatch (hasil scripts/backup.sh, format custom -Fc) ke
# database target via pg_restore.
#
# Semua nilai dari environment — TIDAK ada path/kredensial hardcoded:
#   BACKUP_DIR      (wajib) direktori sumber dump (bisa di-override arg 1)
#   RESTORE_FILE    file dump spesifik; tanpa ini, dump terbaru di BACKUP_DIR
#   APP_DB_URL      URL postgresql://user:pass@host:port/db database TARGET
#                   (bisa di-override arg 2)
#   PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE  alternatif APP_DB_URL
#   PROD_DATABASE   (opsional) nama database produksi yang dilindungi
#
# Keamanan:
#   - Wajib konfirmasi interaktif kecuali diberi --yes (non-interaktif).
#   - Restore ke database yang namanya sama dengan PROD_DATABASE ditolak
#     kecuali flag eksplisit --force-prod.
#
# Contoh (restore ke DB uji):
#   BACKUP_DIR=/var/backups/polywatch APP_DB_URL=postgresql://.../polywatch_test ./restore.sh --yes
set -euo pipefail

readonly Program="restore.sh"

die() {
  printf '%s: %s\n' "$Program" "$*" >&2
  exit 1
}

log() {
  printf '%s: %s\n' "$Program" "$*"
}

usage() {
  sed -n '2,25p' "$0" >&2
  exit 2
}

ASSUME_YES=0
FORCE_PROD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --force-prod) FORCE_PROD=1 ;;
    -h|--help) usage ;;
    -*) die "flag tidak dikenal: $1 (lihat --help)" ;;
    *) break ;;
  esac
  shift
done

POSITIONAL=()
while [ $# -gt 0 ]; do
  POSITIONAL+=("$1")
  shift
done

command -v pg_restore >/dev/null 2>&1 || die "pg_restore tidak ditemukan di PATH"
command -v psql >/dev/null 2>&1 || die "psql tidak ditemukan di PATH"

# --- Sumber & target -----------------------------------------------------------
BACKUP_DIR="${BACKUP_DIR:?env BACKUP_DIR wajib diisi (direktori sumber dump)}"
[ "${#POSITIONAL[@]}" -ge 1 ] && BACKUP_DIR="${POSITIONAL[0]}"
[ -d "$BACKUP_DIR" ] || die "direktori backup tidak ada: ${BACKUP_DIR}"

SLUG="${BACKUP_SLUG:-polywatch}"
if [ "${#POSITIONAL[@]}" -ge 2 ]; then
  TARGET_DB_URL="$POSITIONAL[1]"
elif [ -n "${APP_DB_URL:-}" ]; then
  TARGET_DB_URL="$APP_DB_URL"
elif [ -n "${PGDATABASE:-}" ]; then
  TARGET_DB_URL="$PGDATABASE"
else
  die "database target tidak diketahui: set APP_DB_URL/PGDATABASE atau kirim URL sebagai argumen kedua"
fi

if [ -n "${RESTORE_FILE:-}" ]; then
  DUMP_FILE="$RESTORE_FILE"
else
  # Dump terbaru (nama <slug>-<timestamp>.dump, urut timestamp = urut waktu).
  DUMP_FILE="$(ls -1 "${BACKUP_DIR}/${SLUG}-"*.dump 2>/dev/null | sort | tail -n 1 || true)"
fi
[ -n "$DUMP_FILE" ] && [ -f "$DUMP_FILE" ] \
  || die "file dump tidak ditemukan di ${BACKUP_DIR} (pola ${SLUG}-*.dump)"

# --- Perlindungan database produksi ---------------------------------------------
# Ambil nama DB dari URL/nilai target: segmen path terakhir (buang query params).
TARGET_DBNAME="$(printf '%s' "$TARGET_DB_URL" | sed -E 's#[?].*$##; s#.*/([^/]+)$#\1#')"
case "$TARGET_DBNAME" in
  ?*) : ;;
  *) die "nama database target tidak bisa dibaca dari URL/nilai target" ;;
esac

PROD_DB="${PROD_DATABASE:-polywatch_db}"
if [ "$TARGET_DBNAME" = "$PROD_DB" ] && [ "$FORCE_PROD" -ne 1 ]; then
  die "menolak restore ke database produksi '${PROD_DB}'; gunakan --force-prod bila benar-benar sengaja"
fi

# --- Konfirmasi ------------------------------------------------------------------
if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Restore akan MENIMPA isi database:\n  target : %s\n  dump   : %s\n\nLanjut? [ketik RESTORE]: ' \
    "$TARGET_DBNAME" "$DUMP_FILE"
  read -r ANSWER
  [ "$ANSWER" = "RESTORE" ] || die "konfirmasi ditolak (harus mengetik RESTORE)"
fi

# --- Restore ----------------------------------------------------------------------
log "pg_restore -> ${TARGET_DBNAME} dari ${DUMP_FILE}"

# Bersihkan skema target dulu agar restore deterministik (dump custom berisi
# CREATE TABLE; drop-all menghindari bentrok objek sisa). SELALU menunjuk
# target eksplisit — jangan andalkan PGDATABASE dari environment, yang bisa
# merujuk database lain (mis. DB sumber backup).
psql --dbname "$TARGET_DB_URL" -v ON_ERROR_STOP=1 \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
  || die "gagal membersihkan skema target"

if ! pg_restore --no-owner --no-privileges --dbname "$TARGET_DB_URL" "$DUMP_FILE"; then
  die "pg_restore gagal; database target mungkin setengah terisi — periksa manual"
fi

TABLES="$(psql --dbname "$TARGET_DB_URL" -At \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
log "restore selesai: ${TABLES} tabel di schema public ${TARGET_DBNAME}"
