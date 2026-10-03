"""Safety rules for correcting or deleting (voiding) contribution entries.

Corrections never rewrite settled accounting silently. Before a change is
committed we check, on the server:

  1. The entry's date (old and new) is not inside a month whose historical
     official total has been LOCKED by an Admin. Locked months must be
     unlocked deliberately first (with a recorded reason).
  2. The change does not push a fund's collected-minus-banked figure below
     zero. Banked money is never silently reassigned to another fund or
     removed from collections; if it would be, the correction is refused
     with an explanation.

Both checks are computed from the same services the reports use
(app.services.banking / app.services.totals), so the guard sees exactly
the figures the rest of the app shows.
"""
from app.extensions import db
from app.models import FundType, HistoricalOfficialMonthlyTotal
from app.services.banking import awaiting_banking


class CorrectionBlocked(Exception):
    """Raised when a correction would corrupt locked or banked accounting."""


def _assert_month_unlocked(day):
    locked = HistoricalOfficialMonthlyTotal.query.filter_by(year=day.year, month=day.month, locked=True).first()
    if locked is not None:
        raise CorrectionBlocked(
            f"{day.year}-{day.month:02d} is locked as an official historical total. "
            "Unlock that month in Admin > Historical Official Totals (with a reason) before correcting entries in it."
        )


def _awaiting_by_fund():
    return {fund: awaiting_banking(fund) for fund in FundType}


def apply_guarded_change(affected_dates, mutate):
    """Run ``mutate`` (which changes ORM objects but does not commit) only if
    the result is safe. Raises CorrectionBlocked and leaves the session
    rolled back otherwise."""
    for day in affected_dates:
        _assert_month_unlocked(day)

    before = _awaiting_by_fund()
    mutate()
    db.session.flush()
    after = _awaiting_by_fund()

    for fund in FundType:
        if after[fund] < 0 and after[fund] < before[fund]:
            db.session.rollback()
            label = "Mukululo" if fund == FundType.MUKULULO else "Friday & Sunday"
            raise CorrectionBlocked(
                f"This correction would reduce {label} collections below the amount already banked "
                f"(short by UGX {abs(after[fund]):,}). Banked money cannot be silently moved. "
                "Record a bank adjustment or correct the deposit first."
            )
