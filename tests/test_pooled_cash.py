"""Tests for app.services.pooled_cash - all figures are synthetic."""
from datetime import date

from app.extensions import db
from app.models import (
    HistoricalOfficialMonthlyTotal,
    HistoricalCashMovement,
    CashMovementType,
    TransactionStatus,
    BankAccount,
    BankDeposit,
    FundType,
)
from app.services.pooled_cash import (
    month_gross_pooled,
    month_historical_issued,
    month_pooled_deposited,
    month_adjusted_remaining,
    pooled_historical_issued_all_time,
    overall_awaiting_banking,
)
from app.services.totals import month_official_fund_total


def _historical_row(year, month, mukululo, friday, sunday, issued, admin_user):
    row = HistoricalOfficialMonthlyTotal(
        year=year, month=month, mukululo_total=mukululo, friday_total=friday, sunday_total=sunday,
        historical_issued_allocated=issued, locked=True, created_by_id=admin_user.id,
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestGrossUnchanged(object):
    def test_pooled_gross_equals_sum_of_mukululo_friday_sunday(self, app, admin_user):
        _historical_row(2026, 1, 100000, 20000, 10000, 5000, admin_user)
        assert month_gross_pooled(2026, 1) == 130000
        # Gross-per-fund reporting is completely unaffected by the issued figure.
        assert month_official_fund_total(2026, 1, FundType.MUKULULO) == 100000
        assert month_official_fund_total(2026, 1, FundType.FRIDAY_SUNDAY) == 30000

    def test_setting_issued_allocation_never_changes_stored_gross_columns(self, app, admin_user):
        row = _historical_row(2026, 1, 100000, 20000, 10000, 0, admin_user)
        row.historical_issued_allocated = 50000
        db.session.commit()
        refreshed = db.session.get(HistoricalOfficialMonthlyTotal, row.id)
        assert refreshed.mukululo_total == 100000
        assert refreshed.friday_total == 20000
        assert refreshed.sunday_total == 10000
        assert refreshed.historical_issued_allocated == 50000


class TestAwaitingBankingReduction(object):
    def test_pre_bank_issued_cash_reduces_awaiting_banking(self, app, admin_user):
        _historical_row(2026, 1, 100000, 20000, 10000, 0, admin_user)
        before = overall_awaiting_banking()
        assert before == 130000

        row = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        row.historical_issued_allocated = 40000
        db.session.commit()

        after = overall_awaiting_banking()
        assert after == 130000 - 40000 == 90000

    def test_month_adjusted_remaining_subtracts_issued(self, app, admin_user):
        _historical_row(2026, 1, 100000, 20000, 10000, 40000, admin_user)
        assert month_adjusted_remaining(2026, 1) == 130000 - 40000


class TestIssuedIsNotADeposit(object):
    def test_issued_cash_movement_does_not_create_or_touch_bank_deposits(self, app, admin_user):
        _historical_row(2026, 1, 100000, 20000, 10000, 40000, admin_user)
        movement = HistoricalCashMovement(
            movement_date=date(2026, 1, 15), movement_type=CashMovementType.ISSUED,
            amount=40000, description="test issue", status=TransactionStatus.ACTIVE,
            created_by_id=admin_user.id,
        )
        db.session.add(movement)
        db.session.commit()

        assert BankDeposit.query.count() == 0
        assert month_pooled_deposited(2026, 1) == 0
        # The movement reduces "adjusted remaining" but is never counted
        # as, or confused with, an actual bank deposit.
        assert month_adjusted_remaining(2026, 1) == 100000 + 20000 + 10000 - 40000 - 0

    def test_deposits_and_issued_cash_are_deducted_independently_not_compounded(self, app, admin_user):
        _historical_row(2026, 1, 100000, 20000, 10000, 40000, admin_user)
        account = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()
        db.session.add(BankDeposit(
            deposit_date=date(2026, 1, 20), fund=FundType.MUKULULO,
            bank_account_id=account.id, amount=30000, created_by_id=admin_user.id,
        ))
        db.session.commit()

        # 130,000 gross - 40,000 issued - 30,000 deposited = 60,000 - each
        # deducted exactly once, not doubled or compounded.
        assert month_adjusted_remaining(2026, 1) == 130000 - 40000 - 30000 == 60000
        assert month_pooled_deposited(2026, 1) == 30000
        assert month_historical_issued(2026, 1) == 40000


class TestIssuedDeductedExactlyOnce(object):
    def test_pooled_historical_issued_all_time_sums_each_month_once(self, app, admin_user):
        _historical_row(2026, 1, 100000, 0, 0, 10000, admin_user)
        _historical_row(2026, 2, 100000, 0, 0, 20000, admin_user)
        assert pooled_historical_issued_all_time() == 30000

        # Re-reading/recomputing repeatedly must never accumulate.
        assert pooled_historical_issued_all_time() == 30000
        assert pooled_historical_issued_all_time() == 30000


class TestDecemberExcludedFromYtdButIncludedInRunningBalance(object):
    def test_december_row_does_not_appear_when_summing_2026_months(self, app, admin_user):
        _historical_row(2025, 12, 50000, 5000, 5000, 0, admin_user)
        _historical_row(2026, 1, 100000, 20000, 10000, 0, admin_user)

        ytd_2026_mukululo = sum(
            month_official_fund_total(2026, m, FundType.MUKULULO) for m in range(1, 13)
        )
        # Only January's 100,000 counts - December's 50,000 must not leak in.
        assert ytd_2026_mukululo == 100000

    def test_december_still_contributes_to_all_time_awaiting_banking(self, app, admin_user):
        _historical_row(2025, 12, 50000, 5000, 5000, 0, admin_user)
        _historical_row(2026, 1, 100000, 20000, 10000, 0, admin_user)

        # All-time (cross-year) figure DOES include December's pool.
        assert overall_awaiting_banking() == (50000 + 5000 + 5000) + (100000 + 20000 + 10000)
