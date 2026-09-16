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


class TestMonthlyFifoAllocation(object):
    """Reconciliation/presentation allocation: OLDEST eligible pooled
    month first (FIFO), capped at the issue's own month. This never
    moves or rewrites the actual movement transaction - it only decides
    which months' unbanked gross is shown as consumed by it."""

    def test_oldest_eligible_month_is_consumed_first(self):
        monthly_gross = {(2025, 12): 100, (2026, 1): 200, (2026, 2): 300}
        events = [
            receive(date(2025, 12, 5), 100, (2025, 12)),
            receive(date(2026, 1, 5), 200, (2026, 1)),
            receive(date(2026, 2, 5), 300, (2026, 2)),
            issue(date(2026, 2, 10), 50, (2026, 2)),
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        # December (the oldest eligible month) is drawn on first, even
        # though the issue happened in February.
        assert alloc == {(2025, 12): 50}
        assert result["month_remaining"][(2025, 12)] == 50
        assert result["month_remaining"][(2026, 1)] == 200  # untouched
        assert result["month_remaining"][(2026, 2)] == 300  # untouched (own month, not yet needed)

    def test_oldest_two_months_fully_drained_third_only_partially(self):
        """Mirrors the real correction shape: an issue large enough to
        fully consume the two oldest eligible months and partially
        consume a third, leaving the issue's own (newest eligible)
        month completely untouched."""
        monthly_gross = {(2025, 12): 100, (2026, 1): 400, (2026, 2): 400, (2026, 3): 300}
        events = [
            receive(date(2025, 12, 5), 100, (2025, 12)),
            receive(date(2026, 1, 5), 400, (2026, 1)),
            receive(date(2026, 2, 5), 400, (2026, 2)),
            receive(date(2026, 3, 5), 300, (2026, 3)),
            issue(date(2026, 3, 10), 350, (2026, 3)),  # 100 + 400 covers it with 250 -> Feb needs 250
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]

        assert alloc[(2025, 12)] == 100
        assert alloc[(2026, 1)] == 250
        assert (2026, 2) not in alloc
        assert (2026, 3) not in alloc  # the issue's own month is untouched

        assert result["month_remaining"][(2025, 12)] == 0
        assert result["month_remaining"][(2026, 1)] == 150
        assert result["month_remaining"][(2026, 2)] == 400  # fully untouched
        assert result["month_remaining"][(2026, 3)] == 300  # fully untouched (own gross)

    def test_own_month_only_used_after_older_eligible_balances_exhausted(self):
        monthly_gross = {(2025, 12): 50, (2026, 1): 500}
        events = [
            receive(date(2025, 12, 5), 50, (2025, 12)),
            receive(date(2026, 1, 5), 500, (2026, 1)),
            issue(date(2026, 1, 10), 500, (2026, 1)),  # exceeds December alone
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        assert alloc[(2025, 12)] == 50
        assert alloc[(2026, 1)] == 450  # own month used only for the remainder
        assert result["month_remaining"][(2026, 1)] == 50

    def test_months_after_the_issue_are_never_eligible(self):
        """A month later than the issue's own month must never be
        touched, even if earlier months run out."""
        monthly_gross = {(2026, 1): 10, (2026, 2): 1000}
        events = [
            receive(date(2026, 1, 5), 10, (2026, 1)),
            issue(date(2026, 1, 10), 500, (2026, 1)),  # far more than January has
            receive(date(2026, 2, 5), 1000, (2026, 2)),
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        assert alloc == {(2026, 1): 10}
        assert result["monthly_allocation"][0]["unallocated_shortfall"] == 490
        assert (2026, 2) not in alloc
        assert result["month_remaining"][(2026, 2)] == 1000  # completely untouched

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

    def test_total_allocated_across_months_equals_total_issued(self):
        """The redistribution never changes the total - only where it is
        shown as coming from."""
        monthly_gross = {(2025, 12): 100, (2026, 1): 400, (2026, 2): 400, (2026, 3): 300}
        events = [
            receive(date(2025, 12, 5), 100, (2025, 12)),
            receive(date(2026, 1, 5), 400, (2026, 1)),
            receive(date(2026, 2, 5), 400, (2026, 2)),
            receive(date(2026, 3, 5), 300, (2026, 3)),
            issue(date(2026, 3, 10), 350, (2026, 3)),
        ]
        result = poh.allocate_historical_issues(events, monthly_gross)
        alloc = result["monthly_allocation"][0]["allocated_from"]
        assert sum(alloc.values()) == 350 == events[-1]["amount"]

        total_gross = sum(monthly_gross.values())
        total_remaining = sum(result["month_remaining"].values())
        assert total_gross - total_remaining == 350
