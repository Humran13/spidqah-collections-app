"""Pooled historical cash-position calculations.

During the historical (pre-go-live) period, Mukululo and Friday/Sunday
cash were temporarily pooled together before banking. A pre-bank cash
movement (see HistoricalCashMovement, e.g. money issued out of that
pool before it could be banked) reduces how much of the historical
gross collections is still actually available to bank - without ever
changing the gross collection figures themselves, and without
attributing the outflow to one fund over another (that would be
arbitrary - the source ledger gives no such split).

The exact month-by-month allocation of a historical cash movement
(which months' unbanked gross it was chronologically drawn from) is
computed ONCE, locally, from the day-level spreadsheet ledger - see
scripts/parse_official_history.py - because the app itself only ever
stores MONTHLY aggregates for historical months, not day-level detail.
That verified allocation is what populates
HistoricalOfficialMonthlyTotal.historical_issued_allocated via the
safe import workflow (app.services.history_import). This module just
reads and sums those already-allocated figures - it never re-derives
day-level chronology at runtime.
"""
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models import (
    HistoricalOfficialMonthlyTotal,
    HistoricalCashMovement,
    TransactionStatus,
    BankDeposit,
)
from app.services.totals import month_bounds, month_official_fund_total
from app.models import FundType


def month_gross_pooled(year: int, month: int) -> int:
    """Gross collected across all funds for one month - unaffected by any
    pre-bank issued cash. Mukululo + Friday + Sunday."""
    return month_official_fund_total(year, month, FundType.MUKULULO) + month_official_fund_total(year, month, FundType.FRIDAY_SUNDAY)


def month_historical_issued(year: int, month: int) -> int:
    """How much of this month's gross pooled collection was consumed by a
    pre-bank historical cash movement, per the verified chronological
    allocation. 0 for any month without a locked correction applied."""
    row = HistoricalOfficialMonthlyTotal.query.filter_by(year=year, month=month).first()
    return row.historical_issued_allocated if row else 0


def month_pooled_deposited(year: int, month: int) -> int:
    """Bank deposits (both funds combined - a deposit reduces the pooled
    cash on hand regardless of which fund it is nominally attributed to)
    dated within this month."""
    start, end = month_bounds(year, month)
    total = (
        BankDeposit.query.filter(BankDeposit.deposit_date >= start, BankDeposit.deposit_date <= end)
        .with_entities(func.coalesce(func.sum(BankDeposit.amount), 0))
        .scalar()
    )
    return int(total or 0)


def month_adjusted_remaining(year: int, month: int) -> int:
    """Cash actually still available to bank for this month, after
    deducting pre-bank historical issues and actual deposits. Never
    double-deducts: issued cash and deposits are wholly separate figures
    summed once each."""
    return month_gross_pooled(year, month) - month_historical_issued(year, month) - month_pooled_deposited(year, month)


def pooled_historical_issued_all_time() -> int:
    """Sum of historical_issued_allocated across every historical month on
    record - the total pre-bank outflow that must reduce the all-time
    pooled awaiting-banking figure."""
    total = HistoricalOfficialMonthlyTotal.query.with_entities(
        func.coalesce(func.sum(HistoricalOfficialMonthlyTotal.historical_issued_allocated), 0)
    ).scalar()
    return int(total or 0)


def historical_cash_movements_total(upto: date = None, movement_type=None) -> int:
    """Sum of ACTIVE HistoricalCashMovement amounts - informational/audit
    total of the individual movements themselves (distinct from, but
    expected to reconcile with, pooled_historical_issued_all_time())."""
    q = HistoricalCashMovement.query.filter(HistoricalCashMovement.status == TransactionStatus.ACTIVE)
    if movement_type is not None:
        q = q.filter(HistoricalCashMovement.movement_type == movement_type)
    if upto is not None:
        q = q.filter(HistoricalCashMovement.movement_date <= upto)
    total = q.with_entities(func.coalesce(func.sum(HistoricalCashMovement.amount), 0)).scalar()
    return int(total or 0)


def overall_awaiting_banking() -> int:
    """The corrected, pooled, all-time 'cash that still exists and can
    actually be banked' figure for the dashboard - NOT simply the sum of
    the two per-fund awaiting-banking cards, which deliberately do not
    (and should not) attempt to arbitrarily split the pooled historical
    outflow between funds. See app.services.banking.awaiting_banking for
    the per-fund figures, which remain gross-collected-minus-deposited."""
    from app.services.totals import collected_fund_total_all_time
    from app.services.banking import deposited_fund_total_all_time

    gross = collected_fund_total_all_time(FundType.MUKULULO) + collected_fund_total_all_time(FundType.FRIDAY_SUNDAY)
    deposited = deposited_fund_total_all_time(FundType.MUKULULO) + deposited_fund_total_all_time(FundType.FRIDAY_SUNDAY)
    return gross - pooled_historical_issued_all_time() - deposited
