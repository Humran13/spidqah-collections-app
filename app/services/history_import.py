"""Safe, idempotent import of OFFICIAL historical monthly totals, and of
historical PRE-BANK cash movements (see HistoricalCashMovement).

This is deliberately narrow: it only ever writes to
``HistoricalOfficialMonthlyTotal`` rows (the locked, admin-controlled
source of truth for months before go-live - see app.services.totals)
and to ``HistoricalCashMovement`` rows (pre-bank cash movements that
reduce how much of a month's gross is still available to bank, without
ever changing the gross collection figures themselves). It never
touches individual contributor transactions, never imports a
spreadsheet directly, and never runs outside a human-reviewed
dry-run -> apply sequence.

Expected input payload (already aggregated - no contributor-level data,
no spreadsheet content):

    {
      "source": "free text describing where these figures came from",
      "months": [
        {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000,
         "historical_issued_allocated": 5000},
        ...
      ],
      "issued_movements": [
        {"date": "2026-03-10", "amount": 5000, "description": "..."},
        ...
      ]
    }

``historical_issued_allocated`` and ``issued_movements`` are both
optional; a plain gross-totals-only payload (as used by the original
import) remains valid. When both are present, their totals MUST match
exactly (every movement amount must be allocated to some month, and
vice versa) - see cross_check_issued_total().

Every amount is a non-negative integer number of UGX.
"""
from datetime import date, datetime

from app.extensions import db
from app.models import (
    HistoricalOfficialMonthlyTotal,
    HistoricalCashMovement,
    CashMovementType,
    TransactionStatus,
    AuditAction,
)
from app.services.audit import log_audit

DEFAULT_SOURCE_NOTE = "Imported via flask import-official-history"

ACTION_CREATE = "CREATE"
ACTION_UPDATE = "UPDATE"
ACTION_LOCK_ONLY = "LOCK_ONLY"
ACTION_NO_CHANGE = "NO_CHANGE"
ACTION_CONFLICT = "CONFLICT"
ACTION_ISSUED_UPDATE = "ISSUED_UPDATE"

MOVEMENT_ACTION_CREATE = "CREATE"
MOVEMENT_ACTION_NO_CHANGE = "NO_CHANGE"


class PayloadError(ValueError):
    """Raised for structurally invalid input. Never partially applied."""


def _require_int(value, field, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(f"{field} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise PayloadError(f"{field} must be >= {minimum}, got {value}")
    return value


def validate_payload(data):
    """Validates the ``months`` list only (no DB access). Returns the
    normalized list of month dicts. Raises PayloadError on any
    structural problem - never silently drops or guesses a bad entry."""
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
        issued_allocated = _require_int(
            entry.get("historical_issued_allocated", 0), f"months[{i}].historical_issued_allocated", minimum=0
        )

        key = (year, month)
        if key in seen:
            raise PayloadError(f"Duplicate month in payload: {year}-{month:02d}")
        seen.add(key)

        normalized.append({
            "year": year, "month": month,
            "mukululo": mukululo, "friday": friday, "sunday": sunday,
            "historical_issued_allocated": issued_allocated,
        })

    normalized.sort(key=lambda m: (m["year"], m["month"]))
    return normalized


def validate_movements(data):
    """Validates the optional ``issued_movements`` list. Returns a
    normalized list (possibly empty) of movement dicts with ``date`` as
    a real ``date`` object."""
    if not isinstance(data, dict):
        raise PayloadError("Top-level payload must be a JSON object.")
    movements = data.get("issued_movements", [])
    if movements is None:
        movements = []
    if not isinstance(movements, list):
        raise PayloadError("payload['issued_movements'] must be a list.")

    normalized = []
    for i, entry in enumerate(movements):
        if not isinstance(entry, dict):
            raise PayloadError(f"issued_movements[{i}] must be an object.")
        raw_date = entry.get("date")
        if not isinstance(raw_date, str):
            raise PayloadError(f"issued_movements[{i}].date must be an ISO date string (YYYY-MM-DD).")
        try:
            movement_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise PayloadError(f"issued_movements[{i}].date is not a valid date: {exc}")

        amount = _require_int(entry.get("amount"), f"issued_movements[{i}].amount", minimum=1)

        type_raw = entry.get("movement_type", "ISSUED")
        try:
            movement_type = CashMovementType(type_raw)
        except ValueError:
            raise PayloadError(f"issued_movements[{i}].movement_type must be one of "
                                f"{[t.value for t in CashMovementType]}, got {type_raw!r}")

        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise PayloadError(f"issued_movements[{i}].description must be a string if given.")
        source = entry.get("source")
        if source is not None and not isinstance(source, str):
            raise PayloadError(f"issued_movements[{i}].source must be a string if given.")

        normalized.append({
            "date": movement_date, "amount": amount, "movement_type": movement_type,
            "description": description, "source": source,
        })

    normalized.sort(key=lambda m: (m["date"], m["amount"]))
    return normalized


def cross_check_issued_total(months, movements):
    """Every UGX allocated to a month as historical_issued_allocated must
    be backed by an actual movement, and vice versa - the two totals
    (summed across ISSUED-type movements) must match exactly. Skipped
    (no-op) when both sides are trivially empty/zero, so a plain
    gross-only payload remains valid without this section."""
    allocated_total = sum(m["historical_issued_allocated"] for m in months)
    movement_total = sum(m["amount"] for m in movements if m["movement_type"] == CashMovementType.ISSUED)
    if allocated_total == 0 and movement_total == 0:
        return
    if allocated_total != movement_total:
        raise PayloadError(
            f"historical_issued_allocated across months ({allocated_total:,}) does not match "
            f"the sum of ISSUED issued_movements ({movement_total:,}). Every allocated UGX must "
            f"be backed by an actual movement, and vice versa - refusing to apply an inconsistent payload."
        )


def build_plan(months):
    """Read-only: compares each incoming month against the database and
    decides what WOULD happen. Makes no writes.

    Gross figures (mukululo/friday/sunday) are protected exactly as
    before: a locked month with DIFFERENT gross values is a CONFLICT
    that blocks the whole apply. historical_issued_allocated is a
    separate, always-safe-to-update field (it never changes what was
    collected) and can be updated even on an already-locked month via
    ACTION_ISSUED_UPDATE, as long as the gross values are unchanged.
    """
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
        gross_match = (
            existing_values["mukululo"] == entry["mukululo"]
            and existing_values["friday"] == entry["friday"]
            and existing_values["sunday"] == entry["sunday"]
        )
        issued_match = existing.historical_issued_allocated == entry["historical_issued_allocated"]

        existing_snapshot = {
            "id": existing.id, "locked": existing.locked,
            "historical_issued_allocated": existing.historical_issued_allocated,
            **existing_values,
        }

        if not gross_match and existing.locked:
            # A locked month's GROSS figures represent a deliberately
            # finalized official total. Never silently overwrite them.
            action = ACTION_CONFLICT
        elif not gross_match:
            action = ACTION_UPDATE
        elif not issued_match:
            # Gross is unchanged - updating just the pre-bank issued
            # allocation is always safe, even on a locked row.
            action = ACTION_ISSUED_UPDATE
        else:
            action = ACTION_NO_CHANGE if existing.locked else ACTION_LOCK_ONLY

        plan.append({**entry, "action": action, "existing": existing_snapshot})

    return plan


def build_movement_plan(movements):
    """Read-only. A movement is treated as a duplicate (NO_CHANGE) only
    if an ACTIVE movement with the exact same date+amount+description
    already exists."""
    plan = []
    for m in movements:
        existing = HistoricalCashMovement.query.filter_by(
            movement_date=m["date"], amount=m["amount"], description=m["description"],
            status=TransactionStatus.ACTIVE,
        ).first()
        action = MOVEMENT_ACTION_NO_CHANGE if existing else MOVEMENT_ACTION_CREATE
        plan.append({**m, "action": action, "existing_id": existing.id if existing else None})
    return plan


def _apply_months(plan, user, source_note):
    """Writes the months plan. No commit - caller commits. Raises
    PayloadError (writing nothing) if any item is still a CONFLICT."""
    conflicts = [p for p in plan if p["action"] == ACTION_CONFLICT]
    if conflicts:
        raise PayloadError(
            "Refusing to apply: unresolved conflicts with locked months present. "
            "Unlock the affected month(s) in Admin first if you intend to replace them."
        )

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
                historical_issued_allocated=item["historical_issued_allocated"],
                notes=source_note, locked=True,
                created_by_id=user.id,
            )
            db.session.add(row)
            db.session.flush()
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.CREATE,
                after={"year": year, "month": month, "mukululo": item["mukululo"],
                       "friday": item["friday"], "sunday": item["sunday"],
                       "historical_issued_allocated": item["historical_issued_allocated"], "locked": True},
                reason=source_note, user=user,
            )
            results.append({**item, "applied": "created"})
            continue

        row = db.session.get(HistoricalOfficialMonthlyTotal, item["existing"]["id"])

        if action == ACTION_UPDATE:
            before = {"mukululo": row.mukululo_total, "friday": row.friday_total, "sunday": row.sunday_total,
                      "historical_issued_allocated": row.historical_issued_allocated}
            row.mukululo_total = item["mukululo"]
            row.friday_total = item["friday"]
            row.sunday_total = item["sunday"]
            row.historical_issued_allocated = item["historical_issued_allocated"]
            row.notes = source_note
            row.last_modified_by_id = user.id
            row.last_modified_at = datetime.utcnow()
            after = {"mukululo": row.mukululo_total, "friday": row.friday_total, "sunday": row.sunday_total,
                     "historical_issued_allocated": row.historical_issued_allocated}
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.EDIT,
                before=before, after=after, reason=source_note, user=user,
            )
            if not row.locked:
                row.locked = True
                log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.LOCK, reason=source_note, user=user)
            results.append({**item, "applied": "updated"})
            continue

        if action == ACTION_ISSUED_UPDATE:
            # Gross figures are untouched - only the pre-bank issued
            # allocation changes. Safe even though the row is locked.
            before = {"historical_issued_allocated": row.historical_issued_allocated}
            row.historical_issued_allocated = item["historical_issued_allocated"]
            row.last_modified_by_id = user.id
            row.last_modified_at = datetime.utcnow()
            after = {"historical_issued_allocated": row.historical_issued_allocated}
            log_audit(
                "HistoricalOfficialMonthlyTotal", row.id, AuditAction.EDIT,
                before=before, after=after,
                reason=f"{source_note} (pre-bank issued allocation only - gross totals unchanged)",
                user=user,
            )
            results.append({**item, "applied": "issued_updated"})
            continue

        if action == ACTION_LOCK_ONLY:
            row.locked = True
            row.last_modified_by_id = user.id
            row.last_modified_at = datetime.utcnow()
            log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.LOCK, reason=source_note, user=user)
            results.append({**item, "applied": "locked"})
            continue

    return results


def _apply_movements(plan, user, source_note):
    """Writes the movements plan. No commit - caller commits."""
    results = []
    for item in plan:
        if item["action"] == MOVEMENT_ACTION_NO_CHANGE:
            results.append({**item, "applied": "no_change"})
            continue

        row = HistoricalCashMovement(
            movement_date=item["date"], movement_type=item["movement_type"], amount=item["amount"],
            description=item["description"], source=item["source"] or source_note,
            status=TransactionStatus.ACTIVE, created_by_id=user.id,
        )
        db.session.add(row)
        db.session.flush()
        log_audit(
            "HistoricalCashMovement", row.id, AuditAction.CREATE,
            after={"date": item["date"].isoformat(), "type": item["movement_type"].value,
                   "amount": item["amount"], "description": item["description"]},
            reason=source_note, user=user,
        )
        results.append({**item, "applied": "created"})
    return results


def apply_plan(plan, user, source_note=None):
    """Backward-compatible single-purpose entry point: applies a months
    plan only, and commits. See apply_combined() for months+movements
    applied together atomically."""
    source_note = source_note or DEFAULT_SOURCE_NOTE
    results = _apply_months(plan, user, source_note)
    db.session.commit()
    return results


def apply_movement_plan(plan, user, source_note=None):
    """Backward-compatible single-purpose entry point: applies a
    movements plan only, and commits."""
    source_note = source_note or DEFAULT_SOURCE_NOTE
    results = _apply_movements(plan, user, source_note)
    db.session.commit()
    return results


def apply_combined(months_plan, movements_plan, user, source_note=None):
    """Applies both a months plan and a movements plan in a single
    atomic transaction: if anything fails (e.g. a gross conflict),
    nothing from either plan is written."""
    source_note = source_note or DEFAULT_SOURCE_NOTE
    month_results = _apply_months(months_plan, user, source_note)
    movement_results = _apply_movements(movements_plan, user, source_note)
    db.session.commit()
    return month_results, movement_results
