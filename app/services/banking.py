from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models import (
    BankDeposit,
    BankAdjustment,
    BankAccount,
    FundType,
    AdjustmentDirection,
)
from app.services.totals import collected_fund_total_all_time


def deposited_fund_total_all_time(fund: FundType, upto: date = None) -> int:
    q = BankDeposit.query.filter(BankDeposit.fund == fund)
    if upto is not None:
        q = q.filter(BankDeposit.deposit_date <= upto)
    total = q.with_entities(func.coalesce(func.sum(BankDeposit.amount), 0)).scalar()
    return int(total or 0)


def awaiting_banking(fund: FundType, upto: date = None) -> int:
    collected = collected_fund_total_all_time(fund, upto=upto)
    deposited = deposited_fund_total_all_time(fund, upto=upto)
    return collected - deposited


def deposits_into_account_total(bank_account_id: int, upto: date = None) -> int:
    q = BankDeposit.query.filter(BankDeposit.bank_account_id == bank_account_id)
    if upto is not None:
        q = q.filter(BankDeposit.deposit_date <= upto)
    total = q.with_entities(func.coalesce(func.sum(BankDeposit.amount), 0)).scalar()
    return int(total or 0)


def adjustments_total(bank_account_id: int, upto: date = None) -> int:
    q = BankAdjustment.query.filter(BankAdjustment.bank_account_id == bank_account_id)
    if upto is not None:
        q = q.filter(BankAdjustment.date <= upto)
    increase = q.filter(BankAdjustment.direction == AdjustmentDirection.INCREASE).with_entities(
        func.coalesce(func.sum(BankAdjustment.amount), 0)
    ).scalar()
    decrease = q.filter(BankAdjustment.direction == AdjustmentDirection.DECREASE).with_entities(
        func.coalesce(func.sum(BankAdjustment.amount), 0)
    ).scalar()
    return int(increase or 0) - int(decrease or 0)


def estimated_balance(bank_account: BankAccount, upto: date = None) -> int:
    """opening_balance + deposits into this physical account + adjustments.

    Note: this is the PHYSICAL account balance (based on what was actually
    deposited into it), independent of fund ownership - money can be
    deposited into a bank account that belongs to a different fund.
    """
    opening = bank_account.opening_balance or 0
    deposits = deposits_into_account_total(bank_account.id, upto=upto)
    adj = adjustments_total(bank_account.id, upto=upto)
    return opening + deposits + adj
