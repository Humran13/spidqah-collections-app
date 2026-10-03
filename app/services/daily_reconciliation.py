"""Daily Reconciliation report data (physical cash closings over time).

This is READ-ONLY reporting over the existing CollectionSession records
(the Daily Close). Nothing here writes to the database.

  * Expected cash is the Daily Close's stored category snapshot
    (CollectionSession.grand_total). Those snapshots are kept current by
    app.services.daily_close.refresh_daily_close whenever a contribution is
    corrected or deleted, so the report reflects recalculations without
    running its own accounting.
  * Difference = physical count - expected cash (the same convention the
    Daily Close already stores). Status uses the same BALANCED/SHORT/OVER
    rule as the Daily Close.

Bank Reconciliation is a separate report and is not touched here.
"""
from sqlalchemy.orm import joinedload

from app.models import CollectionSession
from app.services.daily_close import status_for_difference


def closes_in_range(start, end):
    """Daily Closes whose collection date is in [start, end], oldest first.
    Loads the closer in the same query to avoid per-row lookups."""
    return (
        CollectionSession.query.options(joinedload(CollectionSession.created_by))
        .filter(CollectionSession.date >= start, CollectionSession.date <= end)
        .order_by(CollectionSession.date.asc())
        .all()
    )


def close_row(close: CollectionSession) -> dict:
    expected = close.grand_total
    physical = close.physical_cash_counted or 0
    difference = physical - expected
    return {
        "date": close.date,
        "mukululo": close.mukululo_system_total,
        "friday": close.friday_system_total,
        "sunday": close.sunday_system_total,
        "expected": expected,
        "physical": physical,
        "difference": difference,
        "status": status_for_difference(difference).value,
        "closed_by": close.created_by.display_name if close.created_by else "",
        "closed_at": close.created_at,
        "notes": close.notes or "",
    }


def daily_reconciliation_rows(start, end):
    return [close_row(c) for c in closes_in_range(start, end)]


def summarize(rows) -> dict:
    """Totals over the ENTIRE filtered result set (there is no pagination)."""
    summary = {
        "days": len(rows),
        "mukululo": sum(r["mukululo"] for r in rows),
        "friday": sum(r["friday"] for r in rows),
        "sunday": sum(r["sunday"] for r in rows),
        "expected": sum(r["expected"] for r in rows),
        "physical": sum(r["physical"] for r in rows),
        "balanced": 0,
        "short": 0,
        "over": 0,
        "total_short": 0,   # absolute sum of negative day differences
        "total_over": 0,    # sum of positive day differences
    }
    for r in rows:
        summary[r["status"].lower()] += 1
        if r["difference"] < 0:
            summary["total_short"] += -r["difference"]
        elif r["difference"] > 0:
            summary["total_over"] += r["difference"]
    summary["net_difference"] = summary["physical"] - summary["expected"]
    return summary
