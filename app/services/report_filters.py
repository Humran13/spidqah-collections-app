"""Shared date-filter handling for financial reports.

Every report that filters by date goes through ``range_from_args`` so the
rules live in one place:

  * ``exact_date`` (single day) takes precedence over ``date_from`` /
    ``date_to``. When it is supplied and valid, the effective range is
    that one day and From/To are ignored - deterministically, on the
    server, regardless of what the browser sent.
  * Otherwise From/To apply, defaulting to 1 Jan of the current year
    through today (the existing report default).
  * Invalid dates are ignored (falling back to the defaults) rather than
    raising, matching the existing report behaviour.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


def parse_iso_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class ReportRange:
    start: date
    end: date
    exact: Optional[date] = None

    @property
    def is_exact(self) -> bool:
        return self.exact is not None

    def query_args(self) -> dict:
        """Filter parameters to carry through links, pagination and exports."""
        if self.is_exact:
            return {"exact_date": self.exact.isoformat()}
        return {"date_from": self.start.isoformat(), "date_to": self.end.isoformat()}


def range_from_args(args, today: Optional[date] = None) -> ReportRange:
    today = today or date.today()
    exact = parse_iso_date(args.get("exact_date"))
    if exact is not None:
        return ReportRange(start=exact, end=exact, exact=exact)

    start = parse_iso_date(args.get("date_from")) or date(today.year, 1, 1)
    end = parse_iso_date(args.get("date_to")) or today
    return ReportRange(start=start, end=end)


def _int_arg(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _month_bounds(year: int, month: int):
    from calendar import monthrange

    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def resolve_period(args, today: Optional[date] = None):
    """Filter resolution for reports that also offer Month and Year.

    Precedence (deterministic, server-side):
      1. Exact Date            - one day; everything else ignored
      2. From / To             - same rules as range_from_args
      3. Month (+ Year)        - whole calendar month; Year alone = whole year
      4. Default               - the current calendar month

    Returns (ReportRange, filter_args) where filter_args is exactly what
    should be carried into links, pagination and exports.
    """
    today = today or date.today()
    exact = parse_iso_date(args.get("exact_date"))
    if exact is not None:
        return ReportRange(start=exact, end=exact, exact=exact), {"exact_date": exact.isoformat()}

    if args.get("date_from") or args.get("date_to"):
        rng = range_from_args(args, today)
        return rng, rng.query_args()

    month = _int_arg(args.get("month"))
    year = _int_arg(args.get("year"))
    if year is not None and not 2000 <= year <= 2100:
        year = None
    if month is not None and not 1 <= month <= 12:
        month = None

    if month is not None:
        y = year or today.year
        start, end = _month_bounds(y, month)
        return ReportRange(start=start, end=end), {"month": month, "year": y}
    if year is not None:
        return ReportRange(start=date(year, 1, 1), end=date(year, 12, 31)), {"year": year}

    start, end = _month_bounds(today.year, today.month)
    return ReportRange(start=start, end=end), {"month": today.month, "year": today.year}


def export_url(endpoint: str, rng: ReportRange, **extra) -> str:
    """CSV export URL that carries the active filter. Imported lazily so the
    service module stays free of Flask request context requirements."""
    from flask import url_for

    return url_for(endpoint, format="csv", **rng.query_args(), **extra)
