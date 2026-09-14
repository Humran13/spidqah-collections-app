"""Core accounting logic.

Golden rule (see project spec sections 2, 3, 12):

  * ``HistoricalOfficialMonthlyTotal`` rows are the LOCKED, OFFICIAL source
    of truth for whatever portion of a month happened before the go-live
    date. They are entered/edited only by Admin.
  * From the go-live date onward, individual ACTIVE
    ``ContributionTransaction`` rows ARE the official source of truth -
    no separate "official total" row is needed or used for that period.
  * A transaction's date (not which workflow created it) decides whether
    it feeds the official total. In practice, historical back-entries are
    always dated before go-live and therefore never affect official
    totals; live entries are dated on/after go-live and always do.
  * Contributor-detail totals (profiles, rankings, annual statements) sum
    ALL active transactions regardless of date - both live and
    historical back-entries - because they exist to reconstruct
    individual history, not accounting totals.

Money is always an integer number of UGX.
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models import (
    ContributionTransaction,
    TransactionStatus,
    CollectionType,
    FundType,
    FUND_COLLECTION_TYPES,
    HistoricalOfficialMonthlyTotal,
)
from app.services.settings import get_go_live_date


def month_bounds(year: int, month: int):
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _live_transactions_query(collection_types=None, start=None, end=None, contributor_id=None):
    q = ContributionTransaction.query.filter(
        ContributionTransaction.status == TransactionStatus.ACTIVE
    )
    if collection_types:
        q = q.filter(ContributionTransaction.collection_type.in_(collection_types))
    if start is not None:
        q = q.filter(ContributionTransaction.date >= start)
    if end is not None:
        q = q.filter(ContributionTransaction.date <= end)
    if contributor_id is not None:
        q = q.filter(ContributionTransaction.contributor_id == contributor_id)
    return q


def _sum_amount(query):
    total = query.with_entities(func.coalesce(func.sum(ContributionTransaction.amount), 0)).scalar()
    return int(total or 0)


def live_total(collection_type: CollectionType, start=None, end=None, go_live_date=None):
    """Sum of ACTIVE transactions of one collection type dated on/after
    go-live (optionally further bounded by start/end)."""
    go_live_date = go_live_date or get_go_live_date()
    effective_start = max(start, go_live_date) if start else go_live_date
    if end is not None and effective_start > end:
        return 0
    return _sum_amount(_live_transactions_query([collection_type], effective_start, end))


def contributor_detail_total(collection_types=None, start=None, end=None, contributor_id=None):
    """Sum of ALL active transactions regardless of go-live cutover.

    Used for contributor profiles/statements/rankings - informational,
    never used as an accounting/official total.
    """
    return _sum_amount(_live_transactions_query(collection_types, start, end, contributor_id))


def month_official_total(year: int, month: int, collection_type: CollectionType) -> int:
    """Official total for one collection type for a given month.

    = (historical official column, if a row exists for this month) +
      (live transactions of this type dated within the month AND on/after
      go-live).
    """
    go_live_date = get_go_live_date()
    start, end = month_bounds(year, month)

    historical_component = 0
    hist_row = HistoricalOfficialMonthlyTotal.query.filter_by(year=year, month=month).first()
    if hist_row:
        if collection_type == CollectionType.MUKULULO:
            historical_component = hist_row.mukululo_total
        elif collection_type == CollectionType.FRIDAY:
            historical_component = hist_row.friday_total
        elif collection_type == CollectionType.SUNDAY:
            historical_component = hist_row.sunday_total

    live_component = live_total(collection_type, start=start, end=end, go_live_date=go_live_date)
    return historical_component + live_component


def month_official_fund_total(year: int, month: int, fund: FundType) -> int:
    return sum(
        month_official_total(year, month, ct) for ct in FUND_COLLECTION_TYPES[fund]
    )


def is_month_historical(year: int, month: int, go_live_date=None) -> bool:
    """True if the whole month is before the go-live month (i.e. relies
    entirely on the locked historical official total, no live component)."""
    go_live_date = go_live_date or get_go_live_date()
    _, end = month_bounds(year, month)
    return end < go_live_date


def is_month_live_only(year: int, month: int, go_live_date=None) -> bool:
    """True if the whole month is on/after go-live (no historical row
    should normally be needed)."""
    go_live_date = go_live_date or get_go_live_date()
    start, _ = month_bounds(year, month)
    return start >= go_live_date


def historical_fund_total_all_time(fund: FundType) -> int:
    """Sum of every locked/unlocked historical official monthly total for
    this fund, across all months on record. Used for cumulative
    collected-vs-banked calculations."""
    rows = HistoricalOfficialMonthlyTotal.query.all()
    total = 0
    for row in rows:
        if fund == FundType.MUKULULO:
            total += row.mukululo_total
        else:
            total += row.friday_total + row.sunday_total
    return total


def live_fund_total_all_time(fund: FundType, upto: date = None) -> int:
    go_live_date = get_go_live_date()
    types = FUND_COLLECTION_TYPES[fund]
    return sum(
        live_total(ct, start=None, end=upto, go_live_date=go_live_date) for ct in types
    )


def collected_fund_total_all_time(fund: FundType, upto: date = None) -> int:
    """Cumulative official amount ever collected for a fund: locked
    historical totals + all live transactions on/after go-live (optionally
    bounded to `upto`)."""
    return historical_fund_total_all_time(fund) + live_fund_total_all_time(fund, upto=upto)


def chikumi_100_totals(start=None, end=None):
    """Dedicated Chikumi 100 figures - informational detail report only.
    Never added again on top of the Mukululo fund total."""
    from app.models import Contributor, CHIKUMI_100_NAME

    contributor = Contributor.query.filter_by(name_normalized=Contributor.normalize(CHIKUMI_100_NAME)).first()
    if contributor is None:
        return 0
    return contributor_detail_total(
        collection_types=[CollectionType.MUKULULO], start=start, end=end, contributor_id=contributor.id
    )
