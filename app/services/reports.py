from datetime import date

from app.models import Contributor, CollectionType, ContributionTransaction, FundType, FUND_COLLECTION_TYPES
from app.services.totals import contributor_detail_total, month_bounds


def annual_contributor_monthly_breakdown(contributor_id: int, year: int):
    """Returns a list of 12 dicts (Jan..Dec) with mukululo/friday/sunday/total
    for one contributor, using contributor-detail totals (all active
    transactions, live + historical back-entry)."""
    rows = []
    for m in range(1, 13):
        start, end = month_bounds(year, m)
        mukululo = contributor_detail_total([CollectionType.MUKULULO], start, end, contributor_id)
        friday = contributor_detail_total([CollectionType.FRIDAY], start, end, contributor_id)
        sunday = contributor_detail_total([CollectionType.SUNDAY], start, end, contributor_id)
        rows.append({
            "month": m,
            "mukululo": mukululo,
            "friday": friday,
            "sunday": sunday,
            "total": mukululo + friday + sunday,
        })
    return rows


FUND_FILTER_TYPES = {
    "ALL": [CollectionType.MUKULULO, CollectionType.FRIDAY, CollectionType.SUNDAY],
    "MUKULULO": [CollectionType.MUKULULO],
    "FRIDAY_SUNDAY": [CollectionType.FRIDAY, CollectionType.SUNDAY],
}


def annual_contributor_ranking(year: int, fund_filter: str = "ALL", include_zero: bool = True):
    start, end = date(year, 1, 1), date(year, 12, 31)
    types = FUND_FILTER_TYPES.get(fund_filter, FUND_FILTER_TYPES["ALL"])

    contributors = Contributor.query.filter(Contributor.merged_into_id.is_(None)).all()
    results = []
    for c in contributors:
        total = contributor_detail_total(types, start, end, c.id)
        if not include_zero and total <= 0:
            continue
        results.append({"contributor": c, "total": total})
    results.sort(key=lambda r: r["total"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Filtered report rows + grand totals
#
# All figures are computed here, on the server, from the same ACTIVE
# transaction query used by every other report. Grand totals are summed
# over the complete filtered result set (there is no pagination on these
# reports), so they always equal the sum of the rows shown and of the CSV
# export. Voided/deleted transactions are excluded by status.
# ---------------------------------------------------------------------------

CATEGORY_ORDER = (CollectionType.MUKULULO, CollectionType.FRIDAY, CollectionType.SUNDAY)


def _active_amounts_by(group_columns, start, end, collection_types=CATEGORY_ORDER):
    """SUM(amount) of ACTIVE transactions in [start, end], grouped by the
    given columns and collection type. One query, no per-row lookups."""
    from sqlalchemy import func

    from app.extensions import db
    from app.models import ContributionTransaction, TransactionStatus

    columns = [*group_columns, ContributionTransaction.collection_type]
    rows = (
        db.session.query(*columns, func.coalesce(func.sum(ContributionTransaction.amount), 0))
        .filter(
            ContributionTransaction.status == TransactionStatus.ACTIVE,
            ContributionTransaction.collection_type.in_(collection_types),
            ContributionTransaction.date >= start,
            ContributionTransaction.date <= end,
        )
        .group_by(*columns)
        .all()
    )
    return rows


def _category_totals(amounts_by_category):
    totals = {ct.value: 0 for ct in CATEGORY_ORDER}
    for ct in CATEGORY_ORDER:
        totals[ct.value] = int(amounts_by_category.get(ct, 0))
    totals["GRAND"] = sum(totals[ct.value] for ct in CATEGORY_ORDER)
    return totals


def contributor_report_rows(start, end):
    """Per-contributor Mukululo / Friday / Sunday amounts plus filtered
    grand totals for the date range. Includes inactive contributors who
    have amounts in the range, so the rows always reconcile to the totals."""
    from app.models import Contributor

    rows_raw = _active_amounts_by([ContributionTransaction.contributor_id], start, end)
    per_contributor = {}
    overall = {}
    for contributor_id, ctype, amount in rows_raw:
        per_contributor.setdefault(contributor_id, {})[ctype] = int(amount)
        overall[ctype] = overall.get(ctype, 0) + int(amount)

    contributors = {
        c.id: c for c in Contributor.query.filter(Contributor.id.in_(list(per_contributor))).all()
    } if per_contributor else {}

    rows = []
    for contributor_id, amounts in per_contributor.items():
        mukululo = amounts.get(CollectionType.MUKULULO, 0)
        friday = amounts.get(CollectionType.FRIDAY, 0)
        sunday = amounts.get(CollectionType.SUNDAY, 0)
        rows.append({
            "contributor": contributors[contributor_id],
            "mukululo": mukululo,
            "friday": friday,
            "sunday": sunday,
            "total": mukululo + friday + sunday,
        })
    rows.sort(key=lambda r: (-r["total"], r["contributor"].name))
    return rows, _category_totals(overall)


def daily_category_rows(start, end):
    """One row per date with Mukululo / Friday / Sunday columns, plus
    filtered grand totals. Used by the combined report."""
    rows_raw = _active_amounts_by([ContributionTransaction.date], start, end)
    by_day = {}
    overall = {}
    for day, ctype, amount in rows_raw:
        by_day.setdefault(day, {})[ctype] = int(amount)
        overall[ctype] = overall.get(ctype, 0) + int(amount)

    rows = []
    for day in sorted(by_day):
        amounts = by_day[day]
        mukululo = amounts.get(CollectionType.MUKULULO, 0)
        friday = amounts.get(CollectionType.FRIDAY, 0)
        sunday = amounts.get(CollectionType.SUNDAY, 0)
        rows.append({"date": day, "mukululo": mukululo, "friday": friday, "sunday": sunday,
                     "total": mukululo + friday + sunday})
    return rows, _category_totals(overall)
