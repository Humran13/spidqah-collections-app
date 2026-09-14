# SPIDQAH Collections App

A mobile-first web application for recording SPIDQAH's charitable money
collections (Mukululo, and Friday & Sunday), tracking banking/deposits,
reconciling bank balances, and producing contributor and accounting
reports.

## What the system does

- Records individual contributor transactions for three collection types:
  **Mukululo**, **Friday**, **Sunday**.
- Keeps Friday and Sunday distinguishable at the transaction level while
  treating them as one banking fund ("Friday & Sunday").
- Treats **CHIKUMI 100** as an ordinary Mukululo contributor - its amount
  is already inside the Mukululo total and is never added twice. A
  dedicated Chikumi 100 report is available separately for information.
- Separates **official historical accounting totals** (locked, monthly,
  admin-entered - the source of truth for anything before go-live) from
  **historical contributor back-entry** (individual reconstruction for
  contributor history/statements, entered later, which never changes the
  locked totals).
- From the configurable **go-live date** onward, individual transactions
  ARE the live accounting source of truth - no manual recalculation
  needed.
- Tracks **fund ownership** (Mukululo vs Friday & Sunday) separately from
  **physical bank account** the money was deposited into, so a temporary
  cross-deposit never changes who the money belongs to.
- Tracks deposits, bank account opening balances, adjustments, and keeps
  a full history of bank reconciliations (estimated vs actual balance).
- Never hard-deletes financial transactions - edits and voids are
  recorded with a reason in an audit log.
- Produces contributor reports, fund reports, monthly/annual summaries,
  an annual contributor ranking, and printable/PDF annual contributor
  statements (single or batch).

## Architecture

- **Backend:** Python 3.12, Flask (application factory pattern in
  [app/__init__.py](app/__init__.py)), Flask-SQLAlchemy, Flask-Migrate
  (Alembic), Flask-Login, Flask-WTF (CSRF), Flask-Limiter (login rate
  limiting).
- **Database:** PostgreSQL (required - the app deliberately does not
  support SQLite in production; integers only for money, never floats).
- **Frontend:** Server-rendered Jinja templates + Bootstrap 5 (CDN),
  small amount of vanilla JS for the quick collection-entry screen
  (`app/static/js/entry.js`). No SPA framework - kept intentionally
  simple for an internal accountability tool.
- **PDF generation:** ReportLab (annual contributor statements).
- **Containerization:** Docker + Docker Compose (`web` + `postgres`
  services). Postgres is not published to the host; only `web` exposes a
  configurable local port.

### Key modules

- `app/models.py` - all database tables/enums.
- `app/services/totals.py` - the go-live cutover / official-vs-detail
  accounting rules. Read the module docstring first if you need to
  change accounting behavior.
- `app/services/banking.py` - awaiting-banking and estimated bank
  balance calculations.
- `app/services/contributors.py` - contributor search & duplicate-name
  detection.
- `app/services/audit.py` - generic audit log writer used everywhere a
  financially sensitive record is created/edited/voided/locked.
- `app/blueprints/*` - one Flask blueprint per feature area (auth, main
  dashboard, collections entry/history, contributors, banking, reports,
  admin).

### Business rule: go-live cutover (read this before changing totals)

For a given month:

```
official_total(month, type) = historical_official_row[type]   (if a locked/unlocked row exists)
                             + SUM(active transactions of that type
                                   dated within the month AND on/after
                                   the go-live date)
```

Contributor-detail totals (profiles, rankings, annual statements) always
sum **all** active transactions regardless of date - both live entries
and historical back-entries - because they exist to reconstruct
individual history, not accounting totals. This is why a contributor's
profile total and the official monthly total can legitimately differ -
that is expected, not a bug.

## Local development setup (without Docker)

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
```

Note: `psycopg2-binary` requires a C build toolchain on Windows if no
prebuilt wheel is available for your Python version. For local
development without Docker, either install PostgreSQL + build tools, or
just use Docker (recommended - see below). The test suite itself runs
against an in-memory SQLite database and does not need PostgreSQL.

Run the tests:

```bash
FLASK_ENV=testing python -m pytest
```

## Environment variables

Copy `.env.example` to `.env` and fill in real values. Never commit
`.env`. Key variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing key - use a long random string |
| `DATABASE_URL` | SQLAlchemy Postgres URL |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres container credentials |
| `WEB_PORT` | Host port the app is exposed on locally (default 8000) |
| `ORG_NAME` | Organization display name |
| `DEFAULT_GO_LIVE_DATE` | Seed value only - the live value lives in the database and is editable by Admin under Settings |
| `SESSION_COOKIE_SECURE` | Set to `true` once served over HTTPS |
| `FIRST_ADMIN_USERNAME` / `FIRST_ADMIN_EMAIL` / `FIRST_ADMIN_PASSWORD` | First Admin user, created automatically on container start if no matching active Admin exists yet |
| `RATELIMIT_ENABLED` | Login rate limiting on/off |

## Running with Docker (recommended)

```bash
cp .env.example .env
# edit .env: set a real SECRET_KEY, POSTGRES_PASSWORD, and FIRST_ADMIN_PASSWORD

docker compose up -d --build
```

On first start, the entrypoint (`docker-entrypoint.sh`) automatically:

1. Waits for Postgres to be ready.
2. Runs `flask db upgrade` (applies all migrations).
3. Runs `flask init-base-data` (idempotent: seeds app settings, the two
   bank accounts, and the canonical `CHIKUMI 100` contributor - no
   fabricated financial figures).
4. Runs `flask create-admin --non-interactive`, which creates the first
   Admin user from `FIRST_ADMIN_USERNAME` / `FIRST_ADMIN_PASSWORD` if no
   active Admin with that username already exists. It never overwrites
   an existing active Admin's password on restart.

Then visit **http://localhost:8000** (or your configured `WEB_PORT`) and
log in with the `FIRST_ADMIN_*` credentials. Change the password
immediately (top-right menu -> your name -> change password), or create
a personal Admin account and deactivate the shared one.

### Useful Docker commands

```bash
docker compose logs -f web          # tail app logs
docker compose exec web flask shell # interactive shell inside the container
docker compose down                 # stop (keeps data volume)
docker compose down -v              # stop AND wipe the database volume
```

## Database migrations

Migrations live in `migrations/` (standard Flask-Migrate/Alembic
layout) and are generated/run through the `flask db` CLI, inside the
container so the Postgres dialect (`psycopg2`) is available:

```bash
docker compose exec web flask db migrate -m "Describe the change"
docker compose exec web flask db upgrade
```

Review every autogenerated migration before applying it - Alembic
autogenerate is a helpful starting point, not a guarantee, especially
around constraints and enums.

## Creating the first Admin manually

If you did not set `FIRST_ADMIN_*` in `.env`, create one manually:

```bash
docker compose exec web flask create-admin
```

This prompts for a username and password interactively. You can also
promote/reset an existing username to Admin the same way.

## Demo data (development only)

```bash
docker compose exec web flask seed-demo
```

Inserts a handful of contributors and transactions clearly marked as
`DEMO` (in notes and contributor names). **Never run this against a
production database** - it is for exercising the UI locally only. No
real historical financial figures are ever seeded automatically; only
structural data (settings, bank accounts, the `CHIKUMI 100`
contributor) is created by `flask init-base-data`.

## Running tests

```bash
source .venv/Scripts/activate
FLASK_ENV=testing python -m pytest -v
```

The suite (`tests/`) covers the financial rules called out in the spec:
Mukululo including Chikumi 100 exactly once, Friday/Sunday staying
separate while combining correctly, historical back-entry never
changing locked official totals, live post-go-live transactions
affecting official totals, deposits reducing awaiting-banking for the
correct fund, fund ownership surviving a cross-account deposit, voided
transactions being excluded from totals, bank reconciliation
differences, monthly/yearly aggregation, and role-based permissions.

## Backup considerations

- The Postgres data lives in the named Docker volume
  `spidqah_postgres_data`. Back it up regularly, e.g.:
  ```bash
  docker compose exec db pg_dump -U <POSTGRES_USER> <POSTGRES_DB> > backup_$(date +%F).sql
  ```
- Store backups off the VPS (e.g. encrypted, in a separate location).
  This is a financial accountability application - losing the database
  means losing contribution and banking history.
- Test restores periodically:
  ```bash
  cat backup_2026-01-01.sql | docker compose exec -T db psql -U <POSTGRES_USER> <POSTGRES_DB>
  ```

## Later deployment to VPS + CloudPanel (not done yet)

This build stays local-only, as scoped. When ready to deploy:

1. Provision/confirm a CloudPanel site pointing at this app's future
   domain, with a reverse proxy to the `web` container's port.
2. Set `FLASK_ENV=production`, a strong unique `SECRET_KEY`, strong
   database credentials, and `SESSION_COOKIE_SECURE=true` once HTTPS is
   active.
3. Run `docker compose up -d --build` on the VPS from this project
   directory (or via CloudPanel's Docker Compose site type, if used).
4. Point CloudPanel's reverse proxy / Nginx vhost at the host port the
   `web` service publishes (`WEB_PORT`), with HTTPS (Let's Encrypt via
   CloudPanel).
5. Set a real, tested backup schedule for the Postgres volume.
6. Create the real first Admin user, log in, and immediately: set the
   correct go-live date under Admin -> Settings, and set bank account
   opening balances under Banking (each is an audited action).
7. Do **not** run `flask seed-demo` on the production database.

## Historical data import (spreadsheet)

The existing `SPIDQAH 2026.xlsx` in this folder contains a `MUKULULO`
sheet and a `FRI & SUN` sheet, each a running ledger (brought-forward /
received / carried-forward balances per entry, not clean monthly
totals) alongside several unrelated inventory sheets. Because the
figures are a running ledger rather than ready-made monthly totals, and
because turning it into official monthly totals requires judgment calls
this app should not make silently, **no automatic import of this file
was performed**.

The architecture supports safe, controlled entry of official historical
totals: Admin -> Historical Official Totals -> Add Month, one month at
a time, each locked once confirmed and fully audited if later edited.
Individual historical contributor detail (from the physical books) is
entered separately later via Data Entry -> Historical Contributor
Entry, and never affects the locked official totals. If in the future a
bulk-import screen is added (upload -> preview -> dry run -> confirm),
it should write into these same two tables rather than bypassing them.
