#!/usr/bin/env bash
#
# Generates a verified PostgreSQL dump of the SPIDQAH database into the
# persistent backups directory (HOST_BACKUPS_DIR in .env), so it is
# captured by the next CloudPanel/rclone backup run. Intended to run via
# cron shortly before that backup window; see README.md.
#
# Usage (as root, on the VPS):
#   /opt/spidqah-collections-app/scripts/backup-db.sh
#
# Only generates a dump - never modifies CloudPanel's own backup
# configuration.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/spidqah-collections-app}"
ENV_FILE="$REPO_DIR/.env"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }

read_env_var() { grep -m1 "^${1}=" "$ENV_FILE" | cut -d= -f2-; }

POSTGRES_USER="$(read_env_var POSTGRES_USER)"
POSTGRES_DB="$(read_env_var POSTGRES_DB)"
HOST_BACKUPS_DIR="$(read_env_var HOST_BACKUPS_DIR)"

[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_DB" ] || {
  echo "ERROR: POSTGRES_USER / POSTGRES_DB not set in $ENV_FILE." >&2
  exit 1
}
if [ -z "$HOST_BACKUPS_DIR" ]; then
  echo "ERROR: HOST_BACKUPS_DIR is not set in $ENV_FILE." >&2
  echo "This VPS was not installed with CloudPanel storage integration -" >&2
  echo "re-run scripts/deploy-vps.sh with a domain, or set HOST_BACKUPS_DIR" >&2
  echo "by hand to an existing directory you back up." >&2
  exit 1
fi
mkdir -p "$HOST_BACKUPS_DIR"

cd "$REPO_DIR"
TIMESTAMP="$(date +%F_%H%M%S)"
OUT="$HOST_BACKUPS_DIR/spidqah_${TIMESTAMP}.sql.gz"
TMP="${OUT}.tmp"

echo "Dumping ${POSTGRES_DB}..."
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$TMP"

echo "Verifying dump integrity..."
gzip -t "$TMP"
[ -s "$TMP" ] || { echo "ERROR: dump is empty." >&2; rm -f "$TMP"; exit 1; }

mv "$TMP" "$OUT"
chmod 600 "$OUT"
echo "Backup written: $OUT"

# Keep the 14 most recent dumps; older ones are assumed already captured
# by the CloudPanel/rclone backup history.
find "$HOST_BACKUPS_DIR" -maxdepth 1 -name 'spidqah_*.sql.gz' -type f -printf '%T@ %p\n' \
  | sort -rn | tail -n +15 | cut -d' ' -f2- | xargs -r rm -f
