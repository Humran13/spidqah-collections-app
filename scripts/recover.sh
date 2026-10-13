#!/usr/bin/env bash
#
# One-command disaster recovery for SPIDQAH Collections: restores a full
# backup produced by scripts/backup-full.sh (database + uploads + a
# permission fix) onto an installation that scripts/deploy-vps.sh has
# already set up. See DISASTER_RECOVERY.md for the full plain-language
# walkthrough.
#
# Usage (as root, on the VPS, AFTER running the one-line installer):
#   sudo bash scripts/recover.sh --backup /path/to/spidqah_full_<ts>.tar.gz [--force]
#
# --force is only needed if the current database already holds real
# data beyond the installer's own fresh seed - see scripts/restore-db.sh,
# which this script delegates the database restore to unchanged.
#
# What this does, in order, stopping and reporting clearly on any
# failure (never claims success on a partial/failed recovery):
#   1. Validate the backup archive (exists, readable, valid tar.gz,
#      contains manifest.json + db.sql.gz + uploads.tar.gz + env.backup).
#   2. Verify db.sql.gz / uploads.tar.gz against the manifest's sha256
#      checksums - reject anything corrupted or tampered with.
#   3. Check the manifest's PostgreSQL version against the image this
#      deployment actually runs.
#   4. Check free disk space.
#   5. Confirm docker/compose and this deployment's .env are in place.
#   6. Bring the containers up if they are not already running.
#   7. Restore the database via scripts/restore-db.sh (reused as-is: it
#      already refuses to overwrite real data without --force, restores
#      atomically, and auto-recovers on a failed restore).
#   8. Restore uploads - any existing uploads are renamed aside first,
#      never deleted.
#   9. Re-apply the correct, dynamically-detected ownership to both the
#      PostgreSQL data directory and the uploads directory
#      (scripts/lib-permissions.sh - never a hardcoded uid/gid).
#  10. Restart the web container, wait for health, verify HTTP 200, and
#      verify the restored record counts match the manifest's.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/spidqah-collections-app}"
ENV_FILE="$REPO_DIR/.env"
MIN_FREE_DISK_MB=1024
HEALTH_TIMEOUT_SECONDS=180

log()  { printf '\n[recover] %s\n' "$*"; }
die()  { printf '\n[recover] ERROR: %s\n' "$*" >&2; exit 1; }

BACKUP_FILE=""
FORCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --backup) BACKUP_FILE="$2"; shift 2 ;;
    --backup=*) BACKUP_FILE="${1#*=}"; shift ;;
    --force) FORCE="--force"; shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[ -n "$BACKUP_FILE" ] || die "Usage: sudo bash scripts/recover.sh --backup /path/to/spidqah_full_<timestamp>.tar.gz [--force]"
[ "$(id -u)" -eq 0 ] || die "This script must be run as root (e.g. via sudo)."

# ---------------------------------------------------------------------------
# 1. Validate the backup archive.
# ---------------------------------------------------------------------------

log "Validating backup archive: $BACKUP_FILE"
[ -f "$BACKUP_FILE" ] || die "Backup file not found: $BACKUP_FILE"
[ -r "$BACKUP_FILE" ] || die "Backup file is not readable: $BACKUP_FILE"
[ -s "$BACKUP_FILE" ] || die "Backup file is empty: $BACKUP_FILE"
tar -tzf "$BACKUP_FILE" >/dev/null 2>&1 || die "Backup file is not a valid gzip tar archive (corrupted or truncated): $BACKUP_FILE"

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

tar -xzf "$BACKUP_FILE" -C "$WORKDIR" manifest.json db.sql.gz uploads.tar.gz env.backup 2>/dev/null \
  || die "Backup archive is missing required members (manifest.json, db.sql.gz, uploads.tar.gz, env.backup). Not a scripts/backup-full.sh archive?"

for f in manifest.json db.sql.gz uploads.tar.gz env.backup; do
  [ -s "$WORKDIR/$f" ] || die "Backup archive's $f is missing or empty - looks truncated or corrupted."
done

# ---------------------------------------------------------------------------
# 2. Verify checksums from the manifest.
# ---------------------------------------------------------------------------

log "Verifying checksums against the manifest..."
# Extracts a quoted string field's value. Anchored on the opening quote so
# a colon inside the value itself (e.g. an ISO timestamp) is not mistaken
# for the key:value separator.
read_json_field() {
  grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$WORKDIR/manifest.json" | head -1 \
    | sed -E 's/^"[^"]+"[[:space:]]*:[[:space:]]*"([^"]*)"$/\1/'
}

EXPECTED_DB_SHA="$(grep -o '"db\.sql\.gz": *"sha256:[0-9a-f]*"' "$WORKDIR/manifest.json" | grep -o '[0-9a-f]\{64\}')"
EXPECTED_UPLOADS_SHA="$(grep -o '"uploads\.tar\.gz": *"sha256:[0-9a-f]*"' "$WORKDIR/manifest.json" | grep -o '[0-9a-f]\{64\}')"
[ -n "$EXPECTED_DB_SHA" ] && [ -n "$EXPECTED_UPLOADS_SHA" ] || die "Manifest is missing expected checksums - cannot verify this backup's integrity."

ACTUAL_DB_SHA="$(sha256sum "$WORKDIR/db.sql.gz" | cut -d' ' -f1)"
ACTUAL_UPLOADS_SHA="$(sha256sum "$WORKDIR/uploads.tar.gz" | cut -d' ' -f1)"
[ "$ACTUAL_DB_SHA" = "$EXPECTED_DB_SHA" ] || die "db.sql.gz checksum mismatch - the backup is corrupted or was tampered with. Refusing to restore."
[ "$ACTUAL_UPLOADS_SHA" = "$EXPECTED_UPLOADS_SHA" ] || die "uploads.tar.gz checksum mismatch - the backup is corrupted or was tampered with. Refusing to restore."
gzip -t "$WORKDIR/db.sql.gz" 2>/dev/null || die "db.sql.gz failed gzip integrity check."
gzip -t "$WORKDIR/uploads.tar.gz" 2>/dev/null || die "uploads.tar.gz failed gzip integrity check."
tar -tzf "$WORKDIR/uploads.tar.gz" >/dev/null 2>&1 || die "uploads.tar.gz is not a valid tar archive."
log "Checksums verified - backup is intact."

MANIFEST_PG_VERSION="$(read_json_field postgres_version)"
BACKUP_DATE="$(read_json_field backup_date_utc)"
MANIFEST_USERS="$(grep -o '"users": *[0-9]*' "$WORKDIR/manifest.json" | grep -o '[0-9]*$')"
MANIFEST_CONTRIBUTORS="$(grep -o '"contributors": *[0-9]*' "$WORKDIR/manifest.json" | grep -o '[0-9]*$')"
MANIFEST_TRANSACTIONS="$(grep -o '"contribution_transactions": *[0-9]*' "$WORKDIR/manifest.json" | grep -o '[0-9]*$')"
log "Backup is from ${BACKUP_DATE} (PostgreSQL ${MANIFEST_PG_VERSION}); expects users=${MANIFEST_USERS} contributors=${MANIFEST_CONTRIBUTORS} transactions=${MANIFEST_TRANSACTIONS}."

# ---------------------------------------------------------------------------
# 3-5. PostgreSQL compatibility, disk space, environment prerequisites.
# ---------------------------------------------------------------------------

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found. Run the one-line installer (scripts/deploy-vps.sh) first, then re-run recovery."
command -v docker >/dev/null 2>&1 || die "docker is not installed."
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2 plugin) is not available."

cd "$REPO_DIR"
RUNNING_PG_IMAGE="$(docker compose config --images db 2>/dev/null)"
[ -n "$RUNNING_PG_IMAGE" ] || die "Could not determine the PostgreSQL image from docker-compose.yml."
RUNNING_PG_MAJOR="$(echo "$RUNNING_PG_IMAGE" | grep -oE ':[0-9]+' | tr -d ':')"
MANIFEST_PG_MAJOR="$(echo "$MANIFEST_PG_VERSION" | grep -oE '^[0-9]+')"
if [ -n "$RUNNING_PG_MAJOR" ] && [ -n "$MANIFEST_PG_MAJOR" ] && [ "$RUNNING_PG_MAJOR" != "$MANIFEST_PG_MAJOR" ]; then
  die "PostgreSQL version mismatch: backup is from PostgreSQL ${MANIFEST_PG_MAJOR}.x, this deployment runs ${RUNNING_PG_IMAGE} (${RUNNING_PG_MAJOR}.x). A plain-SQL dump restore across major versions is not guaranteed safe - upgrade/downgrade the image to match first."
fi

read_env_var() { grep -m1 "^${1}=" "$ENV_FILE" | cut -d= -f2-; }
HOST_POSTGRES_DIR="$(read_env_var HOST_POSTGRES_DIR)"
HOST_UPLOADS_DIR="$(read_env_var HOST_UPLOADS_DIR)"
[ -n "$HOST_POSTGRES_DIR" ] && [ -n "$HOST_UPLOADS_DIR" ] || die "HOST_POSTGRES_DIR / HOST_UPLOADS_DIR not set in $ENV_FILE. This script requires the CloudPanel storage integration (run scripts/deploy-vps.sh with a domain first)."

CHECK_DIR="$(dirname "$HOST_POSTGRES_DIR")"
[ -d "$CHECK_DIR" ] || CHECK_DIR="/"
AVAIL_MB=$(df --output=avail -m "$CHECK_DIR" | tail -n1 | tr -d ' ')
BACKUP_MB=$(( $(stat -c%s "$BACKUP_FILE") / 1024 / 1024 ))
NEED_MB=$(( BACKUP_MB * 4 + MIN_FREE_DISK_MB ))
if [ "$AVAIL_MB" -lt "$NEED_MB" ]; then
  die "Only ${AVAIL_MB}MB free near ${CHECK_DIR} - need roughly ${NEED_MB}MB (4x the ${BACKUP_MB}MB backup, plus headroom) to restore safely."
fi
log "Disk space OK (${AVAIL_MB}MB free, need ~${NEED_MB}MB)."

source "$REPO_DIR/scripts/lib-permissions.sh"

# ---------------------------------------------------------------------------
# 6. Bring containers up if needed (idempotent).
# ---------------------------------------------------------------------------

log "Ensuring containers are running..."
docker compose up -d >/dev/null
DEADLINE=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
DB_STATUS="unknown"
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  DB_STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q db)" 2>/dev/null || echo "unknown")
  [ "$DB_STATUS" = "healthy" ] && break
  sleep 3
done
[ "$DB_STATUS" = "healthy" ] || die "PostgreSQL did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s. Aborting - nothing has been restored."
log "PostgreSQL is healthy."

# ---------------------------------------------------------------------------
# 7. Restore the database - delegated to scripts/restore-db.sh, unchanged.
#    It already: protects real data unless --force, takes its own safety
#    backup, restores atomically, and auto-recovers on failure.
# ---------------------------------------------------------------------------

log "Restoring database (via scripts/restore-db.sh)..."
if ! REPO_DIR="$REPO_DIR" bash "$REPO_DIR/scripts/restore-db.sh" $FORCE "$WORKDIR/db.sql.gz"; then
  die "Database restore failed (see above). Recovery stopped - uploads were NOT touched."
fi

# ---------------------------------------------------------------------------
# 8. Restore uploads - existing content is renamed aside, never deleted.
# ---------------------------------------------------------------------------

log "Restoring uploads..."
if [ -n "$(find "$HOST_UPLOADS_DIR" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
  PRESERVED="${HOST_UPLOADS_DIR}.pre-recover-$(date -u +%Y%m%d%H%M%S)"
  mv "$HOST_UPLOADS_DIR" "$PRESERVED"
  mkdir -p "$HOST_UPLOADS_DIR"
  log "Existing uploads preserved at: $PRESERVED"
fi
tar -xzf "$WORKDIR/uploads.tar.gz" -C "$HOST_UPLOADS_DIR"

# ---------------------------------------------------------------------------
# 9. Re-apply correct, dynamically-detected ownership.
# ---------------------------------------------------------------------------

log "Repairing permissions..."
# "config --images web" can also list web's dependencies' images (e.g.
# db) depending on the compose version - exclude the already-known db
# image so WEB_IMAGE is never accidentally set to it.
WEB_IMAGE="$(docker compose config --images web 2>/dev/null | grep -vF "$RUNNING_PG_IMAGE" | head -1)"
[ -n "$WEB_IMAGE" ] && fix_uploads_permissions "$HOST_UPLOADS_DIR" "$WEB_IMAGE"
fix_postgres_permissions "$HOST_POSTGRES_DIR" "$RUNNING_PG_IMAGE"

# ---------------------------------------------------------------------------
# 10. Restart, wait for health, verify.
# ---------------------------------------------------------------------------

log "Restarting the web container..."
docker compose restart web >/dev/null
DEADLINE=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
WEB_STATUS="unknown"
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  WEB_STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q web)" 2>/dev/null || echo "unknown")
  [ "$WEB_STATUS" = "healthy" ] && break
  sleep 3
done
[ "$WEB_STATUS" = "healthy" ] || die "Web container did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s after restore. Database was restored; investigate 'docker compose logs web' before assuming the site works."

WEB_PORT="$(read_env_var WEB_PORT)"; WEB_PORT="${WEB_PORT:-8000}"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${WEB_PORT}/auth/login" || echo "000")
[ "$HTTP_CODE" = "200" ] || die "App health check failed: http://127.0.0.1:${WEB_PORT}/auth/login returned HTTP ${HTTP_CODE}. Database was restored; the web app itself needs attention."

run_sql() { docker compose exec -T db psql -U "$(read_env_var POSTGRES_USER)" -d "$(read_env_var POSTGRES_DB)" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
FINAL_USERS="$(run_sql 'SELECT count(*) FROM users;')"
FINAL_CONTRIBUTORS="$(run_sql 'SELECT count(*) FROM contributors;')"
FINAL_TRANSACTIONS="$(run_sql 'SELECT count(*) FROM contribution_transactions;')"

MISMATCH=""
[ -n "$MANIFEST_USERS" ] && [ "$FINAL_USERS" != "$MANIFEST_USERS" ] && MISMATCH="users: expected $MANIFEST_USERS, got $FINAL_USERS"
[ -n "$MANIFEST_CONTRIBUTORS" ] && [ "$FINAL_CONTRIBUTORS" != "$MANIFEST_CONTRIBUTORS" ] && MISMATCH="$MISMATCH; contributors: expected $MANIFEST_CONTRIBUTORS, got $FINAL_CONTRIBUTORS"
[ -n "$MANIFEST_TRANSACTIONS" ] && [ "$FINAL_TRANSACTIONS" != "$MANIFEST_TRANSACTIONS" ] && MISMATCH="$MISMATCH; transactions: expected $MANIFEST_TRANSACTIONS, got $FINAL_TRANSACTIONS"
if [ -n "$MISMATCH" ]; then
  die "Restored record counts do not match the backup's manifest ($MISMATCH). The restore did not produce the expected data - investigate before trusting this instance."
fi

echo ""
echo "############################################################"
echo "  SPIDQAH recovery successful"
echo "############################################################"
echo "Backup restored:   $BACKUP_FILE (${BACKUP_DATE})"
echo "Reachable at:       http://127.0.0.1:${WEB_PORT}/ (put your reverse proxy in front as usual)"
echo "Users:               $FINAL_USERS"
echo "Contributors:        $FINAL_CONTRIBUTORS"
echo "Transactions:        $FINAL_TRANSACTIONS"
echo "Log in with your original credentials from before the disaster."
echo ""
