from datetime import date

import pytest

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    CollectionType,
    TransactionStatus,
    HistoricalOfficialMonthlyTotal,
    BankAccount,
    BankDeposit,
    FundType,
    User,
    CHIKUMI_100_NAME,
)
from app.models import HistoricalCashMovement, CashMovementType
from app.services.history_import import (
    PayloadError,
    validate_payload,
    validate_movements,
    cross_check_issued_total,
    build_plan,
    build_movement_plan,
    apply_plan,
    apply_movement_plan,
    apply_combined,
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_LOCK_ONLY,
    ACTION_NO_CHANGE,
    ACTION_CONFLICT,
    ACTION_ISSUED_UPDATE,
    MOVEMENT_ACTION_CREATE,
    MOVEMENT_ACTION_NO_CHANGE,
)
from app.services.totals import month_official_total, month_official_fund_total


# NOTE: all figures below are arbitrary synthetic test fixtures - they
# have no connection to any real SPIDQAH financial data.
SAMPLE_PAYLOAD = {
    "source": "test import",
    "months": [
        {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000},
        {"year": 2026, "month": 2, "mukululo": 200000, "friday": 30000, "sunday": 15000},
        {"year": 2026, "month": 9, "mukululo": 50000, "friday": 8000, "sunday": 5000},
    ],
}


def _apply_sample(admin_user):
    months = validate_payload(SAMPLE_PAYLOAD)
    plan = build_plan(months)
    return apply_plan(plan, user=admin_user, source_note=SAMPLE_PAYLOAD["source"])


class TestValidation(object):
    def test_rejects_missing_months(self, app):
        with pytest.raises(PayloadError):
            validate_payload({})

    def test_rejects_non_integer_amount(self, app):
        with pytest.raises(PayloadError):
            validate_payload({"months": [{"year": 2026, "month": 1, "mukululo": "100000", "friday": 0, "sunday": 0}]})

    def test_rejects_negative_amount(self, app):
        with pytest.raises(PayloadError):
            validate_payload({"months": [{"year": 2026, "month": 1, "mukululo": -1, "friday": 0, "sunday": 0}]})

    def test_rejects_month_out_of_range(self, app):
        with pytest.raises(PayloadError):
            validate_payload({"months": [{"year": 2026, "month": 13, "mukululo": 0, "friday": 0, "sunday": 0}]})

    def test_rejects_duplicate_month_in_payload(self, app):
        with pytest.raises(PayloadError):
            validate_payload({"months": [
                {"year": 2026, "month": 1, "mukululo": 1, "friday": 0, "sunday": 0},
                {"year": 2026, "month": 1, "mukululo": 2, "friday": 0, "sunday": 0},
            ]})


class TestImportBasics(object):
    def test_mukululo_monthly_total_imports_correctly(self, app, admin_user):
        _apply_sample(admin_user)
        row = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert row is not None
        assert row.mukululo_total == 100000

    def test_friday_and_sunday_import_separately(self, app, admin_user):
        _apply_sample(admin_user)
        row = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert row.friday_total == 20000
        assert row.sunday_total == 10000
        assert row.friday_total != row.sunday_total

    def test_friday_sunday_combined_correctly(self, app, admin_user):
        _apply_sample(admin_user)
        row = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert row.friday_sunday_combined_total == row.friday_total + row.sunday_total == 30000

    def test_imported_rows_are_locked(self, app, admin_user):
        _apply_sample(admin_user)
        rows = HistoricalOfficialMonthlyTotal.query.all()
        assert len(rows) == 3
        assert all(r.locked for r in rows)

    def test_import_writes_only_the_specified_month_not_adjacent_months(self, app, admin_user):
        """Guards against a month/year mixup (e.g. December 2025 data
        leaking into January 2026) - importing January must not create
        or affect any other month's row."""
        months = validate_payload({"months": [
            {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000},
        ]})
        apply_plan(build_plan(months), user=admin_user)

        assert HistoricalOfficialMonthlyTotal.query.filter_by(year=2025, month=12).first() is None
        assert HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=2).first() is None
        jan = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert jan.mukululo_total == 100000


class TestIdempotency(object):
    def test_import_is_idempotent(self, app, admin_user):
        first = _apply_sample(admin_user)
        assert sum(1 for r in first if r["applied"] == "created") == 3

        second = _apply_sample(admin_user)
        assert all(r["applied"] == "no_change" for r in second)
        assert HistoricalOfficialMonthlyTotal.query.count() == 3

    def test_dry_run_makes_zero_database_changes(self, app, admin_user):
        months = validate_payload(SAMPLE_PAYLOAD)
        build_plan(months)  # dry-run equivalent: build_plan never writes
        assert HistoricalOfficialMonthlyTotal.query.count() == 0

        _apply_sample(admin_user)
        assert HistoricalOfficialMonthlyTotal.query.count() == 3

        # A second "dry run" (build_plan only) after data exists still
        # must not change anything.
        plan_again = build_plan(validate_payload(SAMPLE_PAYLOAD))
        assert all(p["action"] == ACTION_NO_CHANGE for p in plan_again)
        assert HistoricalOfficialMonthlyTotal.query.count() == 3

    def test_conflicting_locked_month_blocks_the_entire_apply(self, app, admin_user):
        _apply_sample(admin_user)

        conflicting_payload = {"months": [
            {"year": 2026, "month": 1, "mukululo": 1, "friday": 0, "sunday": 0},  # differs from locked 100000
            {"year": 2026, "month": 3, "mukululo": 50000, "friday": 0, "sunday": 0},  # would otherwise be fine
        ]}
        plan = build_plan(validate_payload(conflicting_payload))
        actions = {p["month"]: p["action"] for p in plan}
        assert actions[1] == ACTION_CONFLICT
        assert actions[3] == ACTION_CREATE

        with pytest.raises(PayloadError):
            apply_plan(plan, user=admin_user)

        # Nothing from this second, conflicting payload was written -
        # March must still not exist.
        assert HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=3).first() is None
        jan = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert jan.mukululo_total == 100000

    def test_unlocked_existing_month_is_safely_updated_and_relocked(self, app, admin_user):
        row = HistoricalOfficialMonthlyTotal(
            year=2026, month=1, mukululo_total=1000, friday_total=0, sunday_total=0,
            locked=False, created_by_id=admin_user.id,
        )
        db.session.add(row)
        db.session.commit()

        plan = build_plan(validate_payload(SAMPLE_PAYLOAD))
        jan_action = next(p["action"] for p in plan if p["month"] == 1)
        assert jan_action == ACTION_UPDATE

        apply_plan(plan, user=admin_user)
        refreshed = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert refreshed.mukululo_total == 100000
        assert refreshed.locked is True


class TestCutoverSafety(object):
    def test_september_historical_does_not_double_count_post_golive_live_transactions(self, app, admin_user):
        """The single most important safety check for this import: a
        locked historical row for the cutover month must ADD to live
        transactions dated on/after go-live, never overwrite or
        double-count them."""
        _apply_sample(admin_user)  # includes September: mukululo=50000

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 50000

        contributor = Contributor(name="Live Sept Contributor", name_normalized="LIVE SEPT CONTRIBUTOR")
        db.session.add(contributor)
        db.session.flush()
        live_txn = ContributionTransaction(
            date=date(2026, 9, 19), collection_type=CollectionType.MUKULULO,
            contributor_id=contributor.id, amount=7000,
            status=TransactionStatus.ACTIVE, created_by_id=admin_user.id,
        )
        db.session.add(live_txn)
        db.session.commit()

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 50000 + 7000

        # Re-running the exact same historical import again must NOT
        # wipe out or affect the live transaction's contribution.
        _apply_sample(admin_user)
        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 50000 + 7000

    def test_historical_backentry_still_cannot_change_official_totals(self, app, admin_user):
        _apply_sample(admin_user)
        assert month_official_total(2026, 1, CollectionType.MUKULULO) == 100000

        contributor = Contributor(name="Back Entry Person", name_normalized="BACK ENTRY PERSON")
        db.session.add(contributor)
        db.session.flush()
        db.session.add(ContributionTransaction(
            date=date(2026, 1, 15), collection_type=CollectionType.MUKULULO,
            contributor_id=contributor.id, amount=999999,
            is_historical_backentry=True, status=TransactionStatus.ACTIVE,
            created_by_id=admin_user.id,
        ))
        db.session.commit()

        assert month_official_total(2026, 1, CollectionType.MUKULULO) == 100000

    def test_chikumi_100_not_double_counted_alongside_an_imported_month(self, app, admin_user):
        """The imported Mukululo figure is a single aggregate that
        already includes Chikumi 100 (per the spreadsheet's own
        accounting) - live Chikumi 100 contributions recorded for a
        LIVE (post-go-live) month must still only be counted once,
        exactly as for any other contributor."""
        _apply_sample(admin_user)

        # The CHIKUMI 100 contributor already exists (seeded by the app
        # fixture, same as in production via `flask init-base-data`).
        chikumi = Contributor.query.filter_by(name_normalized=Contributor.normalize(CHIKUMI_100_NAME)).first()
        abbas = Contributor(name="Abbas", name_normalized="ABBAS")
        db.session.add(abbas)
        db.session.flush()

        db.session.add(ContributionTransaction(
            date=date(2026, 9, 19), collection_type=CollectionType.MUKULULO,
            contributor_id=abbas.id, amount=5000,
            status=TransactionStatus.ACTIVE, created_by_id=admin_user.id,
        ))
        db.session.add(ContributionTransaction(
            date=date(2026, 9, 19), collection_type=CollectionType.MUKULULO,
            contributor_id=chikumi.id, amount=100000,
            status=TransactionStatus.ACTIVE, created_by_id=admin_user.id,
        ))
        db.session.commit()

        # September official = historical (50000) + live (5000 + 100000), Chikumi counted once.
        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 50000 + 5000 + 100000


class TestDoesNotTouchUnrelatedData(object):
    def test_existing_bank_deposits_remain_untouched(self, app, admin_user):
        # The Mukululo Bank Account already exists (seeded by the app
        # fixture, same as in production via `flask init-base-data`).
        account = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()
        deposit = BankDeposit(
            deposit_date=date(2026, 2, 1), fund=FundType.MUKULULO,
            bank_account_id=account.id, amount=50000, created_by_id=admin_user.id,
        )
        db.session.add(deposit)
        db.session.commit()
        deposit_id = deposit.id

        _apply_sample(admin_user)

        still_there = db.session.get(BankDeposit, deposit_id)
        assert still_there is not None
        assert still_there.amount == 50000

    def test_existing_admin_credentials_remain_untouched(self, app, admin_user):
        original_hash = admin_user.password_hash
        original_username = admin_user.username

        _apply_sample(admin_user)

        refreshed = db.session.get(User, admin_user.id)
        assert refreshed.username == original_username
        assert refreshed.password_hash == original_hash
        assert refreshed.active is True


class TestIssuedAllocationValidation(object):
    def test_cross_check_rejects_mismatched_totals(self, app):
        months = validate_payload({"months": [
            {"year": 2026, "month": 1, "mukululo": 100000, "friday": 0, "sunday": 0, "historical_issued_allocated": 5000},
        ]})
        movements = validate_movements({"issued_movements": [
            {"date": "2026-01-10", "amount": 3000, "description": "test"},
        ]})
        with pytest.raises(PayloadError):
            cross_check_issued_total(months, movements)

    def test_cross_check_passes_when_totals_match(self, app):
        months = validate_payload({"months": [
            {"year": 2026, "month": 1, "mukululo": 100000, "friday": 0, "sunday": 0, "historical_issued_allocated": 5000},
        ]})
        movements = validate_movements({"issued_movements": [
            {"date": "2026-01-10", "amount": 5000, "description": "test"},
        ]})
        cross_check_issued_total(months, movements)  # must not raise

    def test_cross_check_is_a_noop_for_plain_gross_only_payload(self, app):
        months = validate_payload(SAMPLE_PAYLOAD)  # no historical_issued_allocated anywhere
        movements = validate_movements(SAMPLE_PAYLOAD)  # no issued_movements key at all
        cross_check_issued_total(months, movements)  # must not raise
        assert movements == []

    def test_rejects_negative_issued_allocated(self, app):
        with pytest.raises(PayloadError):
            validate_payload({"months": [
                {"year": 2026, "month": 1, "mukululo": 0, "friday": 0, "sunday": 0, "historical_issued_allocated": -1},
            ]})

    def test_rejects_invalid_movement_date(self, app):
        with pytest.raises(PayloadError):
            validate_movements({"issued_movements": [{"date": "not-a-date", "amount": 1000}]})

    def test_rejects_zero_or_negative_movement_amount(self, app):
        with pytest.raises(PayloadError):
            validate_movements({"issued_movements": [{"date": "2026-01-10", "amount": 0}]})


class TestIssuedAllocationUpdateOnLockedRow(object):
    """The core new safety behaviour: a month's GROSS totals, once
    locked (e.g. the existing Jan-Sep import), can never be silently
    changed - but its historical_issued_allocated CAN be safely set or
    updated even while locked, because that never changes what was
    collected."""

    def test_issued_only_update_is_allowed_on_an_already_locked_month(self, app, admin_user):
        _apply_sample(admin_user)  # January created+locked, mukululo=100000, issued=0
        jan = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert jan.locked is True
        assert jan.historical_issued_allocated == 0

        correction = {"months": [
            {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000,
             "historical_issued_allocated": 15000},
        ]}
        months = validate_payload(correction)
        plan = build_plan(months)
        assert plan[0]["action"] == ACTION_ISSUED_UPDATE

        apply_plan(plan, user=admin_user)

        refreshed = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert refreshed.locked is True  # still locked
        assert refreshed.mukululo_total == 100000  # gross UNCHANGED
        assert refreshed.friday_total == 20000
        assert refreshed.sunday_total == 10000
        assert refreshed.historical_issued_allocated == 15000  # only this changed

    def test_issued_update_does_not_create_a_duplicate_row(self, app, admin_user):
        _apply_sample(admin_user)
        before_count = HistoricalOfficialMonthlyTotal.query.count()

        correction = {"months": [
            {"year": 2026, "month": 1, "mukululo": 100000, "friday": 20000, "sunday": 10000,
             "historical_issued_allocated": 15000},
        ]}
        plan = build_plan(validate_payload(correction))
        apply_plan(plan, user=admin_user)

        assert HistoricalOfficialMonthlyTotal.query.count() == before_count

    def test_gross_conflict_still_blocks_even_with_issued_allocation_present(self, app, admin_user):
        _apply_sample(admin_user)
        correction = {"months": [
            {"year": 2026, "month": 1, "mukululo": 999, "friday": 20000, "sunday": 10000,
             "historical_issued_allocated": 15000},
        ]}
        plan = build_plan(validate_payload(correction))
        assert plan[0]["action"] == ACTION_CONFLICT
        with pytest.raises(PayloadError):
            apply_plan(plan, user=admin_user)
        unchanged = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert unchanged.mukululo_total == 100000
        assert unchanged.historical_issued_allocated == 0


class TestMovementsWorkflow(object):
    def test_movement_import_is_idempotent(self, app, admin_user):
        movements = validate_movements({"issued_movements": [
            {"date": "2026-03-10", "amount": 5000, "description": "test issue"},
        ]})
        plan1 = build_movement_plan(movements)
        assert plan1[0]["action"] == MOVEMENT_ACTION_CREATE
        apply_movement_plan(plan1, user=admin_user)
        assert HistoricalCashMovement.query.count() == 1

        plan2 = build_movement_plan(movements)
        assert plan2[0]["action"] == MOVEMENT_ACTION_NO_CHANGE
        apply_movement_plan(plan2, user=admin_user)
        assert HistoricalCashMovement.query.count() == 1  # no duplicate

    def test_movement_dry_run_makes_zero_database_changes(self, app, admin_user):
        movements = validate_movements({"issued_movements": [
            {"date": "2026-03-10", "amount": 5000, "description": "test issue"},
        ]})
        build_movement_plan(movements)  # dry-run equivalent
        assert HistoricalCashMovement.query.count() == 0

    def test_combined_apply_is_all_or_nothing_across_months_and_movements(self, app, admin_user):
        _apply_sample(admin_user)

        # January gross conflict alongside an otherwise-valid movement -
        # nothing from either should be written.
        bad_months = build_plan(validate_payload({"months": [
            {"year": 2026, "month": 1, "mukululo": 1, "friday": 0, "sunday": 0},
        ]}))
        good_movements = build_movement_plan(validate_movements({"issued_movements": [
            {"date": "2026-01-10", "amount": 5000, "description": "should not be written"},
        ]}))

        with pytest.raises(PayloadError):
            apply_combined(bad_months, good_movements, user=admin_user)

        assert HistoricalCashMovement.query.count() == 0
        jan = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert jan.mukululo_total == 100000  # untouched
