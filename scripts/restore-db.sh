#!/usr/bin/env bash
#
# Safely restores a verified pg_dump (produced by scripts/backup-db.sh) into
# this instance's PostgreSQL container - for the documented disaster-
# recovery workflow: install SPIDQAH on a new VPS, then restore the old
# database over the fresh one the installer just seeded.
#
# Usage (as root, on the VPS, after scripts/deploy-vps.sh has finished and
# both containers are healthy):
#   scripts/restore-db.sh /path/to/spidqah_<timestamp>.sql.gz
#
# Safety, in order:
#   1. The backup file is fully validated (exists, non-empty, plain pg_dump
#      SQL - gzipped or not - not truncated/corrupted/a different format)
#      before anything in the database is touched.
#   2. The installer seeds a brand-new database with exactly one admin user,
#      one contributor (CHIKUMI 100), and no transactions/deposits/
#      adjustments before you get a chance to restore anything. If the
#      current database holds anything more than that untouched baseline,
#      this script refuses - never import an old database blindly over
#      populated/production tables, and never silently overwrite existing
#      financial records. Pass --force only once you have independently
#      confirmed the target truly is disposable.
#   3. An internal safety backup of whatever is in the database right now
#      is taken and verified before anything is dropped.
#   4. The actual restore (schema drop/recreate + the backup's SQL) runs as
#      one atomic transaction (ON_ERROR_STOP=1, --single-transaction): on
#      any SQL error, PostgreSQL rolls back everything, including the
#      drop/recreate, so a failed restore can never be reported as
#      successful and never leaves a half-restored database.
#   5. If, despite that, the database is left in an unreadable state, this
#      script automatically restores the internal safety backup from step 3
#      to recover the pre-restore state, and says so plainly if even that
#      fails (the safety backup file is kept, never deleted, in that case).

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/spidqah-collections-app}"
ENV_FILE="$REPO_DIR/.env"

die() { echo "ERROR: $*" >&2; exit 1; }

FORCE=false
DUMP_FILE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *) DUMP_FILE="$arg" ;;
  esac
done
[ -n "$DUMP_FILE" ] || die "Usage: $0 [--force] /path/to/spidqah_<timestamp>.sql.gz"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found."

read_env_var() { grep -m1 "^${1}=" "$ENV_FILE" | cut -d= -f2-; }
POSTGRES_USER="$(read_env_var POSTGRES_USER)"
POSTGRES_DB="$(read_env_var POSTGRES_DB)"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_DB" ] || die "POSTGRES_USER / POSTGRES_DB not set in $ENV_FILE."

cd "$REPO_DIR"
run_sql() { docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }

# ---------------------------------------------------------------------------
# 1. Validate the backup file completely before touching the database.
# ---------------------------------------------------------------------------

echo "Validating backup file: $DUMP_FILE"
[ -f "$DUMP_FILE" ] || die "Backup file not found: $DUMP_FILE"
[ -s "$DUMP_FILE" ] || die "Backup file is empty: $DUMP_FILE"

TMP_SQL="$(mktemp)"
cleanup_tmp_sql() { rm -f "$TMP_SQL"; }
trap cleanup_tmp_sql EXIT

case "$DUMP_FILE" in
  *.sql.gz|*.gz)
    gzip -t "$DUMP_FILE" 2>/dev/null || die "Backup file is corrupted or truncated (gzip integrity check failed): $DUMP_FILE"
    gunzip -c "$DUMP_FILE" > "$TMP_SQL" || die "Failed to decompress backup file: $DUMP_FILE"
    ;;
  *.sql)
    cp "$DUMP_FILE" "$TMP_SQL"
    ;;
  *)
    die "Unsupported backup file type: $DUMP_FILE (expected .sql or .sql.gz - the plain-SQL format scripts/backup-db.sh produces, not a custom/tar-format dump)."
    ;;
esac

[ -s "$TMP_SQL" ] || die "Backup file decompressed to empty content: $DUMP_FILE"
if head -c 5 "$TMP_SQL" | grep -q 'PGDMP'; then
  die "Backup is a PostgreSQL custom-format dump, not plain SQL. This script only restores plain-text pg_dump output (psql-compatible), the format scripts/backup-db.sh produces."
fi
grep -q 'PostgreSQL database dump complete' "$TMP_SQL" || die "Backup file does not end with the expected pg_dump completion marker - it looks truncated or corrupted: $DUMP_FILE"
echo "Backup file is valid, complete plain-SQL pg_dump output."

# ---------------------------------------------------------------------------
# 2. Refuse to overwrite a database that already holds real data.
# ---------------------------------------------------------------------------

echo "Checking whether the current database is safe to overwrite..."
USERS=$(run_sql "SELECT count(*) FROM users;" 2>/dev/null | tr -d '[:space:]')
CONTRIBUTORS=$(run_sql "SELECT count(*) FROM contributors;" 2>/dev/null | tr -d '[:space:]')
TRANSACTIONS=$(run_sql "SELECT count(*) FROM contribution_transactions;" 2>/dev/null | tr -d '[:space:]')
DEPOSITS=$(run_sql "SELECT count(*) FROM bank_deposits;" 2>/dev/null | tr -d '[:space:]')
ADJUSTMENTS=$(run_sql "SELECT count(*) FROM bank_adjustments;" 2>/dev/null | tr -d '[:space:]')
RECONCILIATIONS=$(run_sql "SELECT count(*) FROM bank_reconciliations;" 2>/dev/null | tr -d '[:space:]')
USERS="${USERS:-0}"; CONTRIBUTORS="${CONTRIBUTORS:-0}"; TRANSACTIONS="${TRANSACTIONS:-0}"
DEPOSITS="${DEPOSITS:-0}"; ADJUSTMENTS="${ADJUSTMENTS:-0}"; RECONCILIATIONS="${RECONCILIATIONS:-0}"

# A freshly-installed, never-used instance has exactly the installer's own
# seed: one admin user and one contributor (CHIKUMI 100), nothing else.
# Anything beyond that is real data this script must never destroy.
if [ "$TRANSACTIONS" != "0" ] || [ "$DEPOSITS" != "0" ] || [ "$ADJUSTMENTS" != "0" ] || \
   [ "$RECONCILIATIONS" != "0" ] || [ "$USERS" -gt 1 ] || [ "$CONTRIBUTORS" -gt 1 ]; then
  if [ "$FORCE" != true ]; then
    echo "ERROR: this database already contains real data:" >&2
    echo "  users=$USERS contributors=$CONTRIBUTORS transactions=$TRANSACTIONS" >&2
    echo "  deposits=$DEPOSITS adjustments=$ADJUSTMENTS reconciliations=$RECONCILIATIONS" >&2
    echo "Refusing to overwrite it. Re-run with --force ONLY after you have" >&2
    echo "independently confirmed this database is disposable." >&2
    exit 1
  fi
  echo "WARNING: --force given - proceeding to overwrite a non-empty database." >&2
else
  echo "Database still matches the installer's fresh baseline seed - safe to restore over."
fi

# ---------------------------------------------------------------------------
# 3. Take and verify an internal safety backup of the current database.
# ---------------------------------------------------------------------------

echo "Taking a safety backup of the current database before changing anything..."
SAFETY_DUMP="$(mktemp --suffix=.sql.gz)"
if ! docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" 2>/dev/null | gzip > "$SAFETY_DUMP"; then
  rm -f "$SAFETY_DUMP"
  die "Could not create a safety backup of the current database. Aborting - nothing has been changed."
fi
if ! gzip -t "$SAFETY_DUMP" 2>/dev/null || [ ! -s "$SAFETY_DUMP" ]; then
  rm -f "$SAFETY_DUMP"
  die "Safety backup failed verification. Aborting - nothing has been changed."
fi
echo "Safety backup verified: $SAFETY_DUMP"

# ---------------------------------------------------------------------------
# 4. Restore as one atomic transaction - any SQL error rolls back everything
#    (including the schema drop/recreate), so a failure can never leave a
#    half-restored database or be reported as a success.
# ---------------------------------------------------------------------------

restore_sql_file() {
  local sql_file="$1"
  { echo "DROP SCHEMA public CASCADE;"; echo "CREATE SCHEMA public;"; cat "$sql_file"; } \
    | docker compose exec -T db psql -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"
}

echo "Restoring ${DUMP_FILE} (atomic: all-or-nothing)..."
RESTORE_OK=true
restore_sql_file "$TMP_SQL" || RESTORE_OK=false

if [ "$RESTORE_OK" != true ]; then
  echo "Restore failed - PostgreSQL should have rolled back the whole attempt." >&2
  echo "Verifying the database is still readable..." >&2
  CURRENT_USERS=$(run_sql "SELECT count(*) FROM users;" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$CURRENT_USERS" ]; then
    echo "Database is intact (users=${CURRENT_USERS}) - the failed restore was fully rolled back." >&2
    rm -f "$SAFETY_DUMP"
  else
    echo "Database is unreadable after the failed restore attempt. Recovering from the" >&2
    echo "safety backup taken moments ago: $SAFETY_DUMP" >&2
    RECOVERY_OK=true
    restore_sql_file <(gunzip -c "$SAFETY_DUMP") || RECOVERY_OK=false
    if [ "$RECOVERY_OK" = true ] && [ -n "$(run_sql "SELECT count(*) FROM users;" 2>/dev/null | tr -d '[:space:]')" ]; then
      echo "Recovery succeeded - database restored to its pre-restore-attempt state." >&2
      rm -f "$SAFETY_DUMP"
    else
      echo "CRITICAL: automatic recovery also failed. The safety backup has been kept at:" >&2
      echo "  $SAFETY_DUMP" >&2
      echo "Restore it manually: gunzip -c $SAFETY_DUMP | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U $POSTGRES_USER -d $POSTGRES_DB" >&2
    fi
  fi
  die "Restore of ${DUMP_FILE} failed. No success was reported."
fi

# ---------------------------------------------------------------------------
# 5. Only now - after an SQL-error-free, fully-committed restore - verify
#    and report success.
# ---------------------------------------------------------------------------

RESTORED_USERS=$(run_sql "SELECT count(*) FROM users;" 2>/dev/null | tr -d '[:space:]')
if [ -z "$RESTORED_USERS" ] || [ "$RESTORED_USERS" -eq 0 ]; then
  echo "Restore transaction committed but produced zero users - recovering from the safety backup: $SAFETY_DUMP" >&2
  restore_sql_file <(gunzip -c "$SAFETY_DUMP") || echo "CRITICAL: recovery also failed - safety backup kept at $SAFETY_DUMP" >&2
  die "Restore of ${DUMP_FILE} did not produce a usable database. No success was reported."
fi

rm -f "$SAFETY_DUMP"
echo "Restore complete: ${RESTORED_USERS} user(s) recovered. Log in with the original credentials."
echo "Restart the web container so it re-applies any migrations newer than this dump:"
echo "  docker compose restart web"
