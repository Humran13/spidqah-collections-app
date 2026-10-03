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


def export_url(endpoint: str, rng: ReportRange, **extra) -> str:
    """CSV export URL that carries the active filter. Imported lazily so the
    service module stays free of Flask request context requirements."""
    from flask import url_for

    return url_for(endpoint, format="csv", **rng.query_args(), **extra)
