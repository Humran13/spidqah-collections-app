#!/usr/bin/env bash
#
# Full disaster-recovery backup: PostgreSQL dump + uploads + the app's
# own secrets (.env) + a checksummed manifest, bundled into one archive
# under the persistent backups directory (HOST_BACKUPS_DIR in .env).
# This is the file to copy off-site (e.g. to Google Drive) and the one
# scripts/recover.sh consumes. For a lightweight DB-only dump between
# full backups, see scripts/backup-db.sh (unchanged, still used by it
# internally for the dump step).
#
# Usage (as root, on the VPS):
#   scripts/backup-full.sh [--retention N]   (default retention: 7,
#                                              never deletes the last one)
#
# Intended to run via cron shortly before your CloudPanel/rclone backup
# window - see DISASTER_RECOVERY.md. Never modifies CloudPanel's own
# backup configuration.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/spidqah-collections-app}"
ENV_FILE="$REPO_DIR/.env"
RETENTION=7

die() { echo "ERROR: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --retention=*) RETENTION="${arg#*=}" ;;
    --retention) shift ;; # value handled by --retention=N form; ignore bare flag
  esac
done
[[ "$RETENTION" =~ ^[0-9]+$ ]] && [ "$RETENTION" -ge 1 ] || RETENTION=7

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found."
read_env_var() { grep -m1 "^${1}=" "$ENV_FILE" | cut -d= -f2-; }
POSTGRES_USER="$(read_env_var POSTGRES_USER)"
POSTGRES_DB="$(read_env_var POSTGRES_DB)"
HOST_BACKUPS_DIR="$(read_env_var HOST_BACKUPS_DIR)"
HOST_UPLOADS_DIR="$(read_env_var HOST_UPLOADS_DIR)"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_DB" ] || die "POSTGRES_USER/POSTGRES_DB not set in $ENV_FILE."
[ -n "$HOST_BACKUPS_DIR" ] || die "HOST_BACKUPS_DIR not set in $ENV_FILE - run scripts/deploy-vps.sh with a domain first."
mkdir -p "$HOST_BACKUPS_DIR"

cd "$REPO_DIR"
TIMESTAMP="$(date -u +%F_%H%M%S)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Dumping database (consistent logical snapshot via pg_dump)..."
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$WORKDIR/db.sql.gz"
gzip -t "$WORKDIR/db.sql.gz" 2>/dev/null && [ -s "$WORKDIR/db.sql.gz" ] || die "Database dump failed verification - aborting, no backup written."

echo "Archiving uploads..."
if [ -n "$HOST_UPLOADS_DIR" ] && [ -d "$HOST_UPLOADS_DIR" ]; then
  tar -czf "$WORKDIR/uploads.tar.gz" -C "$HOST_UPLOADS_DIR" --ignore-failed-read .
else
  tar -czf "$WORKDIR/uploads.tar.gz" --files-from /dev/null
fi
gzip -t "$WORKDIR/uploads.tar.gz" 2>/dev/null || die "Uploads archive failed verification - aborting, no backup written."

echo "Copying application secrets (.env)..."
cp "$ENV_FILE" "$WORKDIR/env.backup"
chmod 600 "$WORKDIR/env.backup"

echo "Recording table counts for later verification..."
count_table() { docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM $1;" 2>/dev/null | tr -d '[:space:]'; }
USERS_COUNT="$(count_table users)"; USERS_COUNT="${USERS_COUNT:-0}"
CONTRIBUTORS_COUNT="$(count_table contributors)"; CONTRIBUTORS_COUNT="${CONTRIBUTORS_COUNT:-0}"
TRANSACTIONS_COUNT="$(count_table contribution_transactions)"; TRANSACTIONS_COUNT="${TRANSACTIONS_COUNT:-0}"
DEPOSITS_COUNT="$(count_table bank_deposits)"; DEPOSITS_COUNT="${DEPOSITS_COUNT:-0}"

PG_VERSION="$(docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc 'SHOW server_version;' 2>/dev/null | tr -d '[:space:]')"
GIT_COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DB_SHA256="$(sha256sum "$WORKDIR/db.sql.gz" | cut -d' ' -f1)"
UPLOADS_SHA256="$(sha256sum "$WORKDIR/uploads.tar.gz" | cut -d' ' -f1)"

cat > "$WORKDIR/manifest.json" <<EOF
{
  "format_version": 1,
  "backup_date_utc": "$(date -u +%FT%TZ)",
  "postgres_version": "${PG_VERSION:-unknown}",
  "git_commit": "${GIT_COMMIT}",
  "components": ["db.sql.gz", "uploads.tar.gz", "env.backup"],
  "checksums": {
    "db.sql.gz": "sha256:${DB_SHA256}",
    "uploads.tar.gz": "sha256:${UPLOADS_SHA256}"
  },
  "record_counts": {
    "users": ${USERS_COUNT},
    "contributors": ${CONTRIBUTORS_COUNT},
    "contribution_transactions": ${TRANSACTIONS_COUNT},
    "bank_deposits": ${DEPOSITS_COUNT}
  }
}
EOF

OUT="$HOST_BACKUPS_DIR/spidqah_full_${TIMESTAMP}.tar.gz"
TMP_OUT="${OUT}.partial"
tar -czf "$TMP_OUT" -C "$WORKDIR" manifest.json db.sql.gz uploads.tar.gz env.backup
tar -tzf "$TMP_OUT" >/dev/null 2>&1 || { rm -f "$TMP_OUT"; die "Backup archive failed integrity check after assembly - aborting, no backup written."; }
mv "$TMP_OUT" "$OUT"
chmod 600 "$OUT"
echo "Full backup written: $OUT"
echo "  users=${USERS_COUNT} contributors=${CONTRIBUTORS_COUNT} transactions=${TRANSACTIONS_COUNT} deposits=${DEPOSITS_COUNT}"

# Retention: keep the $RETENTION most recent full backups. Since this
# only ever deletes beyond index $RETENTION (>=1), the most recent
# backup is never the one removed, and nothing is deleted at all while
# there are $RETENTION or fewer.
mapfile -t EXISTING < <(find "$HOST_BACKUPS_DIR" -maxdepth 1 -name 'spidqah_full_*.tar.gz' -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
if [ "${#EXISTING[@]}" -gt "$RETENTION" ]; then
  for f in "${EXISTING[@]:$RETENTION}"; do
    rm -f "$f"
  done
fi
