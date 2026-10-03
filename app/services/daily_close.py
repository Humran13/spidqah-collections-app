"""Daily Close / Reconciliation helpers.

One reconciliation date governs a Daily Close: the system totals shown on
the left and the physical cash counted (CollectionSession) on the right
always refer to that same date. Helpers here are shared by the Daily Close
screen and by contribution corrections, which must refresh an existing
close so its snapshot never disagrees with the active transactions.
"""
from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models import (
    CollectionSession,
    ContributionTransaction,
    SessionStatus,
    TransactionStatus,
    AuditAction,
)
from app.services.audit import log_audit

SNAPSHOT_FIELDS = (
    "mukululo_system_total",
    "friday_system_total",
    "sunday_system_total",
    "physical_cash_counted",
    "difference",
    "status",
)


def day_totals(day):
    """Active transaction totals for one date, keyed by category name."""
    rows = (
        db.session.query(ContributionTransaction.collection_type, func.coalesce(func.sum(ContributionTransaction.amount), 0))
        .filter(ContributionTransaction.date == day, ContributionTransaction.status == TransactionStatus.ACTIVE)
        .group_by(ContributionTransaction.collection_type)
        .all()
    )
    totals = {"MUKULULO": 0, "FRIDAY": 0, "SUNDAY": 0}
    for ctype, total in rows:
        totals[ctype.value] = int(total)
    totals["OVERALL"] = totals["MUKULULO"] + totals["FRIDAY"] + totals["SUNDAY"]
    return totals


def _status_for(difference: int) -> SessionStatus:
    if difference == 0:
        return SessionStatus.BALANCED
    if difference > 0:
        return SessionStatus.OVER
    return SessionStatus.SHORT


def apply_snapshot(row: CollectionSession, totals: dict, physical: int):
    """Write system totals, physical count, variance and status onto a close.
    Variance is always computed from the same-day totals passed in."""
    row.mukululo_system_total = totals["MUKULULO"]
    row.friday_system_total = totals["FRIDAY"]
    row.sunday_system_total = totals["SUNDAY"]
    row.physical_cash_counted = physical
    row.difference = physical - totals["OVERALL"]
    row.status = _status_for(row.difference)
    row.updated_at = datetime.utcnow()


def _snapshot(row: CollectionSession):
    return {field: getattr(row, field) for field in SNAPSHOT_FIELDS}


def refresh_daily_close(day, reason: str):
    """If a Daily Close exists for ``day``, recompute its system totals and
    variance from the current active transactions, keeping the physical cash
    count that was entered. Audited. No-op when no close exists for the day."""
    row = CollectionSession.query.filter_by(date=day).first()
    if row is None or row.physical_cash_counted is None:
        return None
    before = _snapshot(row)
    apply_snapshot(row, day_totals(day), row.physical_cash_counted)
    after = _snapshot(row)
    if before != after:
        log_audit("CollectionSession", row.id, AuditAction.EDIT, before=before, after=after, reason=reason)
    return row
