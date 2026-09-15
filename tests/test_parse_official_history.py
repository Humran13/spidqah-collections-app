"""Tests for the pooled-cash chronological allocation logic in
scripts/parse_official_history.py. All figures here are synthetic -
they have no connection to any real SPIDQAH financial data.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import parse_official_history as poh  # noqa: E402


def receive(d, amount, month_key, sheet="TEST"):
    return {"date": d, "sheet": sheet, "row": 1, "kind": "RECEIVE", "amount": amount, "month_key": month_key}


def issue(d, amount, month_key, sheet="TEST", raw=None):
    return {"date": d, "sheet": sheet, "row": 1, "kind": "ISSUE", "amount": amount, "month_key": month_key, "raw": raw}


class TestChronologicalSufficiency(object):
    def test_sufficient_pooled_cash_no_error(self):
        events = [
            receive(date(2026, 1, 5), 1000, (2026, 1)),
            issue(date(2026, 1, 10), 600, (2026, 1)),
        ]
        result = poh.allocate_historical_issues(events, {(2026, 1): 1000})
        assert result["chronological_check"]["error"] is None
        assert result["chronological_check"]["final_pooled_balance"] == 400

    def test_future_receipts_cannot_fund_an_earlier_issue(self):
        """An issue on the 10th must not be treated as funded by money
        that only arrives on the 20th - the chronological walk should
        flag insufficient pooled cash at the moment of the issue."""
        events = [
            receive(date(2026, 1, 5), 100, (2026, 1)),
            issue(date(2026, 1, 10), 600, (2026, 1)),  # only 100 exists yet - should fail
            receive(date(2026, 1, 20), 1000, (2026, 1)),  # arrives AFTER the issue
        ]
        result = poh.allocate_historical_issues(events, {(2026, 1): 1100})
        assert result["chronological_check"]["error"] is not None
        assert "insufficient" in result["chronological_check"]["error"].lower()
        # The balance goes negative right after the issue, not "fixed" by
        # the later receipt retroactively.
        first_issue_balance = result["chronological_check"]["issues_in_order"][0]["pooled_balance_after"]
        assert first_issue_balance == 100 - 600


class TestMonthlyLifoAllocation(object):
    def test_issue_within_its_own_month_is_allocated_to_that_month(self):
        events = [
            receive(date(2026, 3, 1), 500, (2026, 3)),
            issue(date(2026, 3, 10), 300, (2026, 3)),
        ]
        result = poh.allocate_historical_issues(events, {(2026, 3): 500})
        alloc = result["monthly_allocation"][0]["allocated_from"]
        assert alloc == {(2026, 3): 300}
        assert result["month_remaining"][(2026, 3)] == 200

    def test_only_receipts_up_to_the_issue_date_are_eligible_in_the_issue_month(self):
        """Money collected in March AFTER the 10th must not fund a
        10 March issue, even though it is nominally 'March's pool'."""
        events = [
            receive(date(2026, 3, 1), 200, (2026, 3)),
            issue(date(2026, 3, 10), 500, (2026, 3)),
            receive(date(2026, 3, 20), 1000, (2026, 3)),  # arrives after the issue
        ]
        result = poh.allocate_historical_issues(events, {(2026, 3): 1200})
        alloc = result["monthly_allocation"][0]["allocated_from"]
        # Only the 200 available before the issue could be used from
        # March itself; the rest is an unallocated shortfall at the
        # monthly-presentation level (no earlier month exists here).
        assert alloc.get((2026, 3), 0) == 200
        assert result["monthly_allocation"][0]["unallocated_shortfall"] == 300

    def test_newest_eligible_unbanked_cash_consumed_first_spills_to_earlier_months(self):
        """Per the required allocation rule: an issue drains its own
        month's pool first, then the most recent earlier month, and so
        on - never skipping straight to an older month while a newer
        one still has unconsumed cash."""
        monthly_gross = {(2026, 1): 1000, (2026, 2): 1000, (2026, 3): 300}
        events = [
            receive(date(2026, 1, 5), 1000, (2026, 1)),
            receive(date(2026, 2, 5), 1000, (2026, 2)),
            receive(date(2026, 3, 5), 300, (2026, 3)),
            issue(date(2026, 3, 10), 1200, (2026, 3)),  # more than March alone has
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        # March's own 300 first, then February (the newer of the two
        # earlier months) absorbs the remaining 900 - January untouched.
        assert alloc[(2026, 3)] == 300
        assert alloc[(2026, 2)] == 900
        assert (2026, 1) not in alloc
        assert result["month_remaining"][(2026, 1)] == 1000
        assert result["month_remaining"][(2026, 2)] == 100
        assert result["month_remaining"][(2026, 3)] == 0

    def test_pooled_combination_of_receipts_can_fund_an_issue_regardless_of_source(self):
        """Receipts from different 'sheets' (funds) are pooled together
        for this calculation - an issue larger than any single sheet's
        contribution can still be funded by the combined total."""
        events = [
            receive(date(2026, 1, 5), 100, (2026, 1), sheet="MUKULULO"),
            receive(date(2026, 1, 6), 100, (2026, 1), sheet="FRI & SUN"),
            issue(date(2026, 1, 10), 150, (2026, 1)),
        ]
        result = poh.allocate_historical_issues(events, {(2026, 1): 200})
        assert result["chronological_check"]["error"] is None
        assert result["month_remaining"][(2026, 1)] == 50

    def test_earliest_month_absorbs_nothing_when_not_needed(self):
        """Mirrors the real correction: if the newer months' pools are
        enough on their own, an older month (e.g. a brought-forward
        December) must be left completely untouched."""
        monthly_gross = {(2025, 12): 500, (2026, 1): 2000}
        events = [
            receive(date(2025, 12, 5), 500, (2025, 12)),
            receive(date(2026, 1, 5), 2000, (2026, 1)),
            issue(date(2026, 1, 10), 1500, (2026, 1)),
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        assert alloc == {(2026, 1): 1500}
        assert result["month_remaining"][(2025, 12)] == 500  # completely untouched
