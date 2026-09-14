from datetime import date

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    CollectionType,
    TransactionStatus,
    HistoricalOfficialMonthlyTotal,
    CHIKUMI_100_NAME,
)
from app.services.totals import (
    month_official_total,
    month_official_fund_total,
    contributor_detail_total,
    live_total,
)
from app.models import FundType


def add_contributor(name):
    c = Contributor(name=name, name_normalized=Contributor.normalize(name))
    db.session.add(c)
    db.session.commit()
    return c


def add_txn(contributor, collection_type, amount, day, historical=False, status=TransactionStatus.ACTIVE, user=None):
    t = ContributionTransaction(
        date=day,
        collection_type=collection_type,
        contributor_id=contributor.id,
        amount=amount,
        is_historical_backentry=historical,
        status=status,
        created_by_id=user.id if user else None,
    )
    db.session.add(t)
    db.session.commit()
    return t


class TestChikumi100(object):
    def test_mukululo_includes_chikumi_100_exactly_once(self, app, admin_user):
        chikumi = Contributor.query.filter_by(name_normalized=Contributor.normalize(CHIKUMI_100_NAME)).first()
        abbas = add_contributor("Abbas")
        day = date(2026, 9, 20)  # after go-live (2026-09-18)

        add_txn(abbas, CollectionType.MUKULULO, 5000, day, user=admin_user)
        add_txn(chikumi, CollectionType.MUKULULO, 100000, day, user=admin_user)

        mukululo_total = month_official_total(2026, 9, CollectionType.MUKULULO)
        assert mukululo_total == 105000

        # Chikumi's detail total should not be double counted anywhere else.
        chikumi_only = contributor_detail_total([CollectionType.MUKULULO], contributor_id=chikumi.id)
        assert chikumi_only == 100000
        assert mukululo_total == 5000 + chikumi_only


class TestFridaySunday(object):
    def test_friday_and_sunday_stay_separate_but_combine(self, app, admin_user):
        abbas = add_contributor("Abbas")
        day = date(2026, 9, 20)
        add_txn(abbas, CollectionType.FRIDAY, 20000, day, user=admin_user)
        add_txn(abbas, CollectionType.SUNDAY, 15000, day, user=admin_user)

        friday = month_official_total(2026, 9, CollectionType.FRIDAY)
        sunday = month_official_total(2026, 9, CollectionType.SUNDAY)
        assert friday == 20000
        assert sunday == 15000

        combined = month_official_fund_total(2026, 9, FundType.FRIDAY_SUNDAY)
        assert combined == friday + sunday == 35000


class TestGoLiveCutover(object):
    def test_pre_golive_backentry_does_not_change_locked_official_totals(self, app, admin_user):
        # Lock August 2026 official totals (entirely before go-live 2026-09-18).
        row = HistoricalOfficialMonthlyTotal(
            year=2026, month=8, mukululo_total=500000, friday_total=100000, sunday_total=80000,
            locked=True, created_by_id=admin_user.id,
        )
        db.session.add(row)
        db.session.commit()

        official_before = month_official_total(2026, 8, CollectionType.MUKULULO)
        assert official_before == 500000

        abbas = add_contributor("Abbas")
        add_txn(abbas, CollectionType.MUKULULO, 7777, date(2026, 8, 15), historical=True, user=admin_user)

        official_after = month_official_total(2026, 8, CollectionType.MUKULULO)
        assert official_after == 500000, "Historical back-entry must not change the locked official total"

        # But it DOES show up in contributor-detail totals.
        assert contributor_detail_total([CollectionType.MUKULULO], contributor_id=abbas.id) == 7777

    def test_post_golive_contribution_affects_live_financial_totals(self, app, admin_user):
        abbas = add_contributor("Abbas")
        day = date(2026, 9, 18)  # exactly go-live date
        add_txn(abbas, CollectionType.MUKULULO, 5000, day, user=admin_user)

        assert live_total(CollectionType.MUKULULO) == 5000
        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 5000

    def test_cutover_month_combines_historical_and_live_portions(self, app, admin_user):
        # September 2026: go-live is the 18th. Historical row represents
        # the pre-golive portion (1st-17th); live transactions from the
        # 18th onward add on top.
        row = HistoricalOfficialMonthlyTotal(
            year=2026, month=9, mukululo_total=200000, friday_total=0, sunday_total=0,
            locked=True, created_by_id=admin_user.id,
        )
        db.session.add(row)
        db.session.commit()

        abbas = add_contributor("Abbas")
        add_txn(abbas, CollectionType.MUKULULO, 20000, date(2026, 9, 18), user=admin_user)

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 220000


class TestVoid(object):
    def test_voided_transaction_stops_counting(self, app, admin_user):
        abbas = add_contributor("Abbas")
        day = date(2026, 9, 20)
        txn = add_txn(abbas, CollectionType.MUKULULO, 5000, day, user=admin_user)

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 5000

        txn.status = TransactionStatus.VOID
        db.session.commit()

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 0
        assert contributor_detail_total([CollectionType.MUKULULO], contributor_id=abbas.id) == 0


class TestMonthlyYearlyTotals(object):
    def test_monthly_and_yearly_totals_calculate_correctly(self, app, admin_user):
        abbas = add_contributor("Abbas")
        add_txn(abbas, CollectionType.MUKULULO, 1000, date(2026, 9, 20), user=admin_user)
        add_txn(abbas, CollectionType.MUKULULO, 2000, date(2026, 9, 25), user=admin_user)
        add_txn(abbas, CollectionType.MUKULULO, 3000, date(2026, 10, 5), user=admin_user)

        assert month_official_total(2026, 9, CollectionType.MUKULULO) == 3000
        assert month_official_total(2026, 10, CollectionType.MUKULULO) == 3000

        yearly = contributor_detail_total(
            [CollectionType.MUKULULO], date(2026, 1, 1), date(2026, 12, 31), contributor_id=abbas.id
        )
        assert yearly == 6000
