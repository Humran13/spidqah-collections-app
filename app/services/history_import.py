"""Safe, idempotent import of OFFICIAL historical monthly totals.

This is deliberately narrow: it only ever writes to
``HistoricalOfficialMonthlyTotal`` rows (the locked, admin-controlled
source of truth for months before go-live - see app.services.totals).
It never touches individual contributor transactions, never imports a
spreadsheet directly, and never runs outside a human-reviewed
dry-run -> apply sequence.

Expected input payload (already aggregated - no contributor-level data,
no spreadsheet content):

    {
      "source": "free text describing where these figures came from",
      "months": [
        {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000},
        ...
      ]
    }

Every amount is a non-negative integer number of UGX.
"""
from datetime import datetime

from app.extensions import db
from app.models import HistoricalOfficialMonthlyTotal, AuditAction
from app.services.audit import log_audit

DEFAULT_SOURCE_NOTE = "Imported via flask import-official-history"

ACTION_CREATE = "CREATE"
ACTION_UPDATE = "UPDATE"
ACTION_LOCK_ONLY = "LOCK_ONLY"
ACTION_NO_CHANGE = "NO_CHANGE"
ACTION_CONFLICT = "CONFLICT"


class PayloadError(ValueError):
    """Raised for structurally invalid input. Never partially applied."""


def _require_int(value, field, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(f"{field} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise PayloadError(f"{field} must be >= {minimum}, got {value}")
    return value


def validate_payload(data):
    """Validates structure only (no DB access). Returns the normalized
    list of month dicts. Raises PayloadError on any structural problem -
    never silently drops or guesses a bad entry."""
    if not isinstance(data, dict):
        raise PayloadError("Top-level payload must be a JSON object.")
    months = data.get("months")
    if not isinstance(months, list) or not months:
        raise PayloadError("payload['months'] must be a non-empty list.")

    seen = set()
    normalized = []
    for i, entry in enumerate(months):
        if not isinstance(entry, dict):
            raise PayloadError(f"months[{i}] must be an object.")
        year = _require_int(entry.get("year"), f"months[{i}].year", minimum=2000)
        month = _require_int(entry.get("month"), f"months[{i}].month", minimum=1)
        if month > 12:
            raise PayloadError(f"months[{i}].month must be 1-12, got {month}")
        mukululo = _require_int(entry.get("mukululo"), f"months[{i}].mukululo", minimum=0)
        friday = _require_int(entry.get("friday"), f"months[{i}].friday", minimum=0)
        sunday = _require_int(entry.get("sunday"), f"months[{i}].sunday", minimum=0)

        key = (year, month)
        if key in seen:
            raise PayloadError(f"Duplicate month in payload: {year}-{month:02d}")
        seen.add(key)

        normalized.append({
            "year": year, "month": month,
            "mukululo": mukululo, "friday": friday, "sunday": sunday,
        })

    normalized.sort(key=lambda m: (m["year"], m["month"]))
    return normalized


def build_plan(months):
    """Read-only: compares each incoming month against the database and
    decides what WOULD happen. Makes no writes."""
    plan = []
    for entry in months:
        existing = HistoricalOfficialMonthlyTotal.query.filter_by(
            year=entry["year"], month=entry["month"]
        ).first()

        if existing is None:
            plan.append({**entry, "action": ACTION_CREATE, "existing": None})
            continue

        existing_values = {
            "mukululo": existing.mukululo_total,
            "friday": existing.friday_total,
            "sunday": existing.sunday_total,
        }
        values_match = (
            existing_values["mukululo"] == entry["mukululo"]
            and existing_values["friday"] == entry["friday"]
            and existing_values["sunday"] == entry["sunday"]
        )

        existing_snapshot = {
            "id": existing.id, "locked": existing.locked, **existing_values,
        }

        if values_match:
            action = ACTION_NO_CHANGE if existing.locked else ACTION_LOCK_ONLY
        elif existing.locked:
            # A locked month represents a deliberately finalized figure.
            # Never silently overwrite it - surface the conflict instead.
            action = ACTION_CONFLICT
        else:
            action = ACTION_UPDATE

        plan.append({**entry, "action": action, "existing": existing_snapshot})

    return plan


def apply_plan(plan, user, source_note=None):
    """Writes the plan to the database. Caller MUST have already checked
    that no item has action == CONFLICT (see PayloadError-free contract
    below) - this function refuses to run otherwise, as a second line of
    defence against partial/unsafe application."""
    conflicts = [p for p in plan if p["action"] == ACTION_CONFLICT]
    if conflicts:
        raise PayloadError(
            "Refusing to apply: unresolved conflicts with locked months present. "
            "Unlock the affected month(s) in Admin first if you intend to replace them."
        )

    source_note = source_note or DEFAULT_SOURCE_NOTE
    results = []

    for item in plan:
        action = item["action"]
        year, month = item["year"], item["month"]

        if action == ACTION_NO_CHANGE:
            results.append({**item, "applied": "no_change"})
            continue

        if action == ACTION_CREATE:
            row = HistoricalOfficialMonthlyTotal(
                year=year, month=month,
                mukululo_total=item["mukululo"], friday_total=item["friday"], sunday_total=item["sunday"],
                notes=source_note, locked=True,
                created_by_id=user.id,
            )
            db.session.add(row)
            db.session.flush()
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.CREATE,
                after={"year": year, "month": month, "mukululo": item["mukululo"],
                       "friday": item["friday"], "sunday": item["sunday"], "locked": True},
                reason=source_note, user=user,
            )
            results.append({**item, "applied": "created"})
            continue

        # UPDATE or LOCK_ONLY both operate on an existing, currently-unlocked row.
        row = db.session.get(HistoricalOfficialMonthlyTotal, item["existing"]["id"])

        if action == ACTION_UPDATE:
            before = {"mukululo": row.mukululo_total, "friday": row.friday_total, "sunday": row.sunday_total}
            row.mukululo_total = item["mukululo"]
            row.friday_total = item["friday"]
            row.sunday_total = item["sunday"]
            row.notes = source_note
            row.last_modified_by_id = user.id
            row.last_modified_at = datetime.utcnow()
            after = {"mukululo": row.mukululo_total, "friday": row.friday_total, "sunday": row.sunday_total}
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.EDIT,
                before=before, after=after, reason=source_note, user=user,
            )

        if not row.locked:
            row.locked = True
            row.last_modified_by_id = user.id
            row.last_modified_at = datetime.utcnow()
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.LOCK,
                reason=source_note, user=user,
            )

        results.append({**item, "applied": "updated" if action == ACTION_UPDATE else "locked"})

    db.session.commit()
    return results
