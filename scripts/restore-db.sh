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
# The installer seeds a brand-new database with exactly one admin user, one
# contributor (CHIKUMI 100), and no transactions/deposits/adjustments before
# you get a chance to restore anything. This script checks for that: if the
# database still looks like that untouched baseline, it drops and recreates
# the schema and restores the dump cleanly. If it finds anything more (real
# users, contributors, transactions, deposits, adjustments,
# reconciliations), it refuses to touch it - never import an old database
# blindly over populated/production tables, and never silently overwrite
# existing financial records. Pass --force only once you have independently
# confirmed the target truly is disposable.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-/opt/spidqah-collections-app}"
ENV_FILE="$REPO_DIR/.env"

FORCE=false
DUMP_FILE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *) DUMP_FILE="$arg" ;;
  esac
done
if [ -z "$DUMP_FILE" ] || [ ! -f "$DUMP_FILE" ]; then
  echo "Usage: $0 [--force] /path/to/spidqah_<timestamp>.sql.gz" >&2
  exit 1
fi
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }

read_env_var() { grep -m1 "^${1}=" "$ENV_FILE" | cut -d= -f2-; }
POSTGRES_USER="$(read_env_var POSTGRES_USER)"
POSTGRES_DB="$(read_env_var POSTGRES_DB)"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_DB" ] || {
  echo "ERROR: POSTGRES_USER / POSTGRES_DB not set in $ENV_FILE." >&2
  exit 1
}

cd "$REPO_DIR"
run_sql() { docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }

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

echo "Dropping and recreating the public schema (clean target for restore)..."
run_sql "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null

echo "Restoring ${DUMP_FILE}..."
case "$DUMP_FILE" in
  *.gz) gunzip -c "$DUMP_FILE" | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" ;;
  *)    docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$DUMP_FILE" ;;
esac

RESTORED_USERS=$(run_sql "SELECT count(*) FROM users;" | tr -d '[:space:]')
[ "${RESTORED_USERS:-0}" -gt 0 ] || { echo "ERROR: restore produced zero users - something is wrong." >&2; exit 1; }
echo "Restore complete: ${RESTORED_USERS} user(s) recovered. Log in with the original credentials."
echo "Restart the web container so it re-applies any migrations newer than this dump:"
echo "  docker compose restart web"
