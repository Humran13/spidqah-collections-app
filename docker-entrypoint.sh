#!/bin/bash
set -e

echo "Waiting for database..."
python - <<'PYEOF'
import os
import sys
import time
import psycopg2

url = os.environ.get("DATABASE_URL", "")
if url.startswith("postgresql"):
    for attempt in range(30):
        try:
            conn = psycopg2.connect(url.replace("+psycopg2", ""))
            conn.close()
            print("Database is ready.")
            break
        except Exception as exc:
            print(f"Database not ready yet ({attempt + 1}/30): {exc}")
            time.sleep(2)
    else:
        print("Database never became ready.", file=sys.stderr)
        sys.exit(1)
PYEOF

# Persistent upload directory (PWA logo). Created here so a fresh named
# volume is always writable by the app user.
UPLOAD_DIR="${UPLOAD_DIR:-/app/instance/uploads}"
mkdir -p "$UPLOAD_DIR"
if [ ! -w "$UPLOAD_DIR" ]; then
  echo "Upload directory $UPLOAD_DIR is not writable by $(id -un)." >&2
  exit 1
fi

echo "Running database migrations..."
flask db upgrade

echo "Ensuring base data (settings, bank accounts, CHIKUMI 100)..."
flask init-base-data

if [ -n "$FIRST_ADMIN_USERNAME" ] && [ -n "$FIRST_ADMIN_PASSWORD" ]; then
  echo "Ensuring first Admin user exists..."
  flask create-admin --non-interactive || true
fi

exec "$@"
