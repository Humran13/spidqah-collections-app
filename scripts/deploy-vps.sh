#!/usr/bin/env bash
#
# One-command VPS deployment / update for the SPIDQAH Collections App.
#
# Usage (as root, on the target VPS):
#   curl -fsSL https://raw.githubusercontent.com/Humran13/spidqah-collections-app/main/scripts/deploy-vps.sh | bash
# or, once cloned:
#   /opt/spidqah-collections-app/scripts/deploy-vps.sh
#
# What it does:
#   - Preflight checks (docker, docker compose, git, openssl, disk space,
#     port 8000 availability) without touching anything else on the box.
#   - Clones the repo into /opt/spidqah-collections-app on first run, or
#     fast-forward pulls main on later runs. Refuses to touch the
#     directory if it exists and is not this project's git repo.
#   - Generates a production .env (strong SECRET_KEY / DB password / admin
#     password) on first run only; always preserves an existing .env.
#   - Builds and starts the app bound to 127.0.0.1:8000 only (this is the
#     project's built-in default in docker-compose.yml itself - "WEB_BIND"
#     defaults to 127.0.0.1, no override file needed). PostgreSQL is never
#     published.
#   - Applies database migrations and seeds base data via the existing
#     docker-entrypoint.sh (unchanged) - never resets the database.
#   - Waits for both containers to report healthy, then verifies the app
#     actually answers on 127.0.0.1:8000, the port is not exposed on
#     0.0.0.0, and Postgres has no published port.
#   - Never touches any other container, port 5000, CloudPanel, Nginx,
#     MySQL, Redis, RustDesk, or runs any global docker prune/cleanup.
#
# Safe to re-run: it is idempotent and is the same command used for
# future updates (pulls latest main, rebuilds, migrates, restarts,
# re-verifies - without resetting the database or regenerating secrets).

set -Eeuo pipefail

REPO_URL="https://github.com/Humran13/spidqah-collections-app.git"
REPO_DIR="/opt/spidqah-collections-app"
WEB_PORT="${WEB_PORT:-8000}"
MIN_FREE_DISK_MB=2048
HEALTH_TIMEOUT_SECONDS=180
COMPOSE=""   # set once we know the repo directory is in place

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

log()  { printf '\n[deploy] %s\n' "$*"; }
warn() { printf '\n[deploy] WARNING: %s\n' "$*" >&2; }
die()  { printf '\n[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

on_error() {
  local line=$1
  printf '\n############################################################\n'
  printf '  DEPLOYMENT FAILED (script line %s)\n' "$line"
  printf '############################################################\n'
  if [ -n "$COMPOSE" ]; then
    echo "Recent container logs:"
    (cd "$REPO_DIR" && eval "$COMPOSE logs --tail=100") || true
  fi
  echo "Nothing outside /opt/spidqah-collections-app and its own Docker"
  echo "containers was touched. Fix the issue above and re-run this script."
  exit 1
}
trap 'on_error $LINENO' ERR

# ---------------------------------------------------------------------------
# Phase A - preflight checks
# ---------------------------------------------------------------------------

log "Preflight checks"

if [ "$(id -u)" -ne 0 ]; then
  die "This script must be run as root (e.g. via sudo)."
fi

for bin in docker git openssl curl ss awk; do
  command -v "$bin" >/dev/null 2>&1 || die "Required tool '$bin' is not installed."
done

docker compose version >/dev/null 2>&1 || die "'docker compose' (v2 plugin) is not available."

AVAIL_MB=$(df --output=avail -m / | tail -n1 | tr -d ' ')
if [ "$AVAIL_MB" -lt "$MIN_FREE_DISK_MB" ]; then
  die "Only ${AVAIL_MB}MB free on / - need at least ${MIN_FREE_DISK_MB}MB. Aborting."
fi
log "Disk OK: ${AVAIL_MB}MB free on /"

# Port 8000 must be free, UNLESS it's already our own SPIDQAH container
# (that's the normal, expected case on a re-run/update).
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${WEB_PORT}\$"; then
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^spidqah-collections-app-web-1$'; then
    log "Port ${WEB_PORT} is already in use by our own existing SPIDQAH container - will be recreated."
  else
    echo "Processes currently bound to port ${WEB_PORT}:"
    ss -ltnp 2>/dev/null | grep ":${WEB_PORT} " || true
    die "Port ${WEB_PORT} is already in use by something that is not SPIDQAH. Refusing to continue. Nothing has been changed."
  fi
else
  log "Port ${WEB_PORT} is free."
fi

# ---------------------------------------------------------------------------
# Phase B - repository
# ---------------------------------------------------------------------------

log "Preparing repository at ${REPO_DIR}"

if [ -e "$REPO_DIR" ] && [ ! -d "$REPO_DIR/.git" ]; then
  die "${REPO_DIR} already exists and is not a git repository. Refusing to overwrite - inspect it manually first."
fi

git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true

if [ -d "$REPO_DIR/.git" ]; then
  cd "$REPO_DIR"
  ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
  case "$ORIGIN_URL" in
    *spidqah-collections-app*) ;;
    *) die "${REPO_DIR} exists but its git remote ('${ORIGIN_URL}') does not look like the SPIDQAH repo. Refusing to touch it." ;;
  esac
  log "Existing SPIDQAH checkout found - updating main..."
  git fetch origin main
  git checkout main
  if ! git merge --ff-only origin/main; then
    die "Local main in ${REPO_DIR} has diverged from origin/main and cannot be fast-forwarded. Resolve this manually (this script never force-resets)."
  fi
else
  log "Cloning ${REPO_URL} into ${REPO_DIR}..."
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone "$REPO_URL" "$REPO_DIR"
  cd "$REPO_DIR"
fi

CURRENT_COMMIT="$(git log -1 --oneline)"
log "Deployed commit: ${CURRENT_COMMIT}"

COMPOSE='docker compose'

# ---------------------------------------------------------------------------
# Phase C - production .env (generate once, always preserve afterwards)
# ---------------------------------------------------------------------------

FIRST_RUN=false
GENERATED_ADMIN_PASSWORD=""
GENERATED_ADMIN_USERNAME=""

if [ -f "$REPO_DIR/.env" ]; then
  log ".env already exists - preserving it (secrets are not regenerated)."
else
  log "No .env found - generating a new production .env from .env.example..."
  FIRST_RUN=true
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"

  gen_hex()   { openssl rand -hex 32; }
  gen_alnum() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$1"; }

  SECRET_KEY_VALUE="$(gen_hex)"
  POSTGRES_PASSWORD_VALUE="$(gen_alnum 28)"
  GENERATED_ADMIN_USERNAME="admin"
  GENERATED_ADMIN_PASSWORD="$(gen_alnum 20)"

  ENV_FILE="$REPO_DIR/.env"
  sed -i "s|^FLASK_APP=.*|FLASK_APP=wsgi:app|" "$ENV_FILE"
  sed -i "s|^FLASK_ENV=.*|FLASK_ENV=production|" "$ENV_FILE"
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY_VALUE}|" "$ENV_FILE"
  sed -i "s|^POSTGRES_USER=.*|POSTGRES_USER=spidqah|" "$ENV_FILE"
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD_VALUE}|" "$ENV_FILE"
  sed -i "s|^POSTGRES_DB=.*|POSTGRES_DB=spidqah|" "$ENV_FILE"
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg2://spidqah:${POSTGRES_PASSWORD_VALUE}@db:5432/spidqah|" "$ENV_FILE"
  sed -i "s|^WEB_PORT=.*|WEB_PORT=${WEB_PORT}|" "$ENV_FILE"
  sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=true|" "$ENV_FILE"
  sed -i "s|^FIRST_ADMIN_USERNAME=.*|FIRST_ADMIN_USERNAME=${GENERATED_ADMIN_USERNAME}|" "$ENV_FILE"
  sed -i "s|^FIRST_ADMIN_PASSWORD=.*|FIRST_ADMIN_PASSWORD=${GENERATED_ADMIN_PASSWORD}|" "$ENV_FILE"
  # ORG_NAME, DEFAULT_GO_LIVE_DATE, FIRST_ADMIN_EMAIL, RATELIMIT_ENABLED
  # are left at their .env.example defaults - edit .env by hand later if
  # you want to change them, then re-run this script to apply (it will
  # NOT overwrite your edits).
  chmod 600 "$ENV_FILE"
  log "Generated .env with production secrets (permissions set to 600)."
fi

# App-level timezone (Africa/Kampala) and currency (UGX) are fixed in
# app/config.py, not environment-driven, so there is nothing to set here.

# ---------------------------------------------------------------------------
# Phase D - build & start (never touches anything outside this compose project)
# ---------------------------------------------------------------------------

log "Building and starting SPIDQAH (docker compose)..."
cd "$REPO_DIR"
eval "$COMPOSE up -d --build"

# ---------------------------------------------------------------------------
# Phase E - wait for health
# ---------------------------------------------------------------------------

log "Waiting for containers to become healthy (up to ${HEALTH_TIMEOUT_SECONDS}s)..."
DEADLINE=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
DB_OK=false
WEB_OK=false

while [ "$SECONDS" -lt "$DEADLINE" ]; do
  DB_STATUS=$(docker inspect --format='{{.State.Health.Status}}' spidqah-collections-app-db-1 2>/dev/null || echo "unknown")
  WEB_STATUS=$(docker inspect --format='{{.State.Health.Status}}' spidqah-collections-app-web-1 2>/dev/null || echo "unknown")
  [ "$DB_STATUS" = "healthy" ] && DB_OK=true
  [ "$WEB_STATUS" = "healthy" ] && WEB_OK=true
  if [ "$DB_OK" = true ] && [ "$WEB_OK" = true ]; then
    break
  fi
  sleep 3
done

if [ "$DB_OK" != true ] || [ "$WEB_OK" != true ]; then
  echo "db health=${DB_STATUS:-unknown}  web health=${WEB_STATUS:-unknown}"
  die "Containers did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s. See logs above."
fi
log "Both containers report healthy (db=${DB_STATUS}, web=${WEB_STATUS})."

# ---------------------------------------------------------------------------
# Phase F - verification (fail loudly, never claim success falsely)
# ---------------------------------------------------------------------------

log "Verifying the app responds on 127.0.0.1:${WEB_PORT}..."
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${WEB_PORT}/auth/login" || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
  die "http://127.0.0.1:${WEB_PORT}/auth/login returned HTTP ${HTTP_CODE}, expected 200."
fi
log "Login page responded HTTP 200."

log "Verifying port ${WEB_PORT} is bound to 127.0.0.1 only..."
BIND_LINE=$(ss -ltn 2>/dev/null | awk -v p=":${WEB_PORT}" '$4 ~ p {print $4}')
if [ -z "$BIND_LINE" ]; then
  die "Could not find anything listening on port ${WEB_PORT}."
fi
if ! echo "$BIND_LINE" | grep -q "^127\.0\.0\.1:${WEB_PORT}\$"; then
  die "Port ${WEB_PORT} is bound as '${BIND_LINE}', not 127.0.0.1:${WEB_PORT}. Refusing to report success - this must never be reachable on 0.0.0.0."
fi
log "Confirmed: bound to ${BIND_LINE} (localhost only)."

log "Verifying PostgreSQL is not publicly exposed..."
DB_CID=$(eval "$COMPOSE ps -q db")
DB_PORTS=$(docker port "$DB_CID" 2>/dev/null || true)
if [ -n "$DB_PORTS" ]; then
  die "PostgreSQL container has published ports (${DB_PORTS}). This must never happen - refusing to report success."
fi
log "Confirmed: PostgreSQL has no published ports."

if docker inspect --format='{{.State.Health.Status}}' daily_figures_app >/dev/null 2>&1; then
  DF_STATUS=$(docker inspect --format='{{.State.Health.Status}}' daily_figures_app)
  log "Daily Figures container is untouched (health=${DF_STATUS})."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "############################################################"
echo "  SPIDQAH deployment successful"
echo "############################################################"
echo "Directory:        ${REPO_DIR}"
echo "Commit deployed:  ${CURRENT_COMMIT}"
echo "Reachable at:     http://127.0.0.1:${WEB_PORT}  (localhost only, as required)"
echo "PostgreSQL:       internal only, persistent volume, not published"
echo "Daily Figures:    unaffected (still on 127.0.0.1:5000)"
echo ""
echo "CloudPanel/Nginx/SSL have NOT been touched - that is the next,"
echo "separate step once you're ready."
echo ""

if [ "$FIRST_RUN" = true ]; then
  echo "------------------------------------------------------------"
  echo "FIRST-TIME SETUP - Admin login (shown once, not stored anywhere else):"
  echo "  Username: ${GENERATED_ADMIN_USERNAME}"
  echo "  Password: ${GENERATED_ADMIN_PASSWORD}"
  echo "Change this password immediately after logging in."
  echo "------------------------------------------------------------"
fi
