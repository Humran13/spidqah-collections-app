from datetime import date

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    CollectionType,
    TransactionStatus,
    BankAccount,
    BankDeposit,
    BankReconciliation,
    FundType,
    ReconciliationStatus,
)
from app.services.banking import awaiting_banking, estimated_balance
from app.services.totals import month_official_total


def add_contributor(name):
    c = Contributor(name=name, name_normalized=Contributor.normalize(name))
    db.session.add(c)
    db.session.commit()
    return c


def add_txn(contributor, collection_type, amount, day, user):
    t = ContributionTransaction(
        date=day, collection_type=collection_type, contributor_id=contributor.id,
        amount=amount, status=TransactionStatus.ACTIVE, created_by_id=user.id,
    )
    db.session.add(t)
    db.session.commit()
    return t


class TestDepositReducesAwaitingBanking(object):
    def test_deposit_reduces_amount_awaiting_banking_for_ownership_fund(self, app, admin_user):
        abbas = add_contributor("Abbas")
        add_txn(abbas, CollectionType.MUKULULO, 1000000, date(2026, 9, 20), admin_user)

        assert awaiting_banking(FundType.MUKULULO) == 1000000

        mukululo_account = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()
        deposit = BankDeposit(
            deposit_date=date(2026, 9, 21), fund=FundType.MUKULULO,
            bank_account_id=mukululo_account.id, amount=700000, created_by_id=admin_user.id,
        )
        db.session.add(deposit)
        db.session.commit()

        assert awaiting_banking(FundType.MUKULULO) == 300000


class TestOwnershipVsPhysicalDestination(object):
    def test_depositing_friday_sunday_money_into_mukululo_account_keeps_ownership(self, app, admin_user):
        abbas = add_contributor("Abbas")
        add_txn(abbas, CollectionType.FRIDAY, 50000, date(2026, 9, 20), admin_user)

        mukululo_account = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()

        # Friday/Sunday money physically deposited into the Mukululo bank account
        # (temporary situation described in the spec) must NOT change ownership.
        deposit = BankDeposit(
            deposit_date=date(2026, 9, 22), fund=FundType.FRIDAY_SUNDAY,
            bank_account_id=mukululo_account.id, amount=50000, created_by_id=admin_user.id,
        )
        db.session.add(deposit)
        db.session.commit()

        # Ownership fund (Friday & Sunday) awaiting-banking has gone down...
        assert awaiting_banking(FundType.FRIDAY_SUNDAY) == 0
        # ...but Mukululo's own collected/awaiting figures are untouched by this deposit.
        assert awaiting_banking(FundType.MUKULULO) == 0  # nothing collected for mukululo in this test
        assert deposit.fund == FundType.FRIDAY_SUNDAY
        assert deposit.bank_account_id == mukululo_account.id

        # The physical account balance DOES reflect the money that landed there.
        assert estimated_balance(mukululo_account) == 50000


class TestBankReconciliation(object):
    def test_reconciliation_difference_is_calculated_correctly(self, app, admin_user):
        mukululo_account = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()
        mukululo_account.opening_balance = 100000
        mukululo_account.opening_balance_date = date(2026, 1, 1)
        db.session.commit()

        deposit = BankDeposit(
            deposit_date=date(2026, 9, 21), fund=FundType.MUKULULO,
            bank_account_id=mukululo_account.id, amount=50000, created_by_id=admin_user.id,
        )
        db.session.add(deposit)
        db.session.commit()

        estimate = estimated_balance(mukululo_account)
        assert estimate == 150000

        actual_balance = 148000
        difference = actual_balance - estimate
        status = ReconciliationStatus.RECONCILED if difference == 0 else ReconciliationStatus.DIFFERENCE

        recon = BankReconciliation(
            bank_account_id=mukululo_account.id, balance_date=date(2026, 9, 30),
            actual_balance=actual_balance, estimated_balance_snapshot=estimate,
            difference=difference, status=status, created_by_id=admin_user.id,
        )
        db.session.add(recon)
        db.session.commit()

        assert recon.difference == -2000
        assert recon.status == ReconciliationStatus.DIFFERENCE

        # A perfectly matching balance is RECONCILED.
        recon2 = BankReconciliation(
            bank_account_id=mukululo_account.id, balance_date=date(2026, 10, 1),
            actual_balance=150000, estimated_balance_snapshot=150000,
            difference=0, status=ReconciliationStatus.RECONCILED, created_by_id=admin_user.id,
        )
        db.session.add(recon2)
        db.session.commit()
        assert recon2.status == ReconciliationStatus.RECONCILED
