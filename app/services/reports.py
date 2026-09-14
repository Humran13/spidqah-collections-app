from datetime import date

from app.models import Contributor, CollectionType, FundType, FUND_COLLECTION_TYPES
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
