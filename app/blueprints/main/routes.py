from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.models import (
    CollectionType,
    FundType,
    ContributionTransaction,
    TransactionStatus,
    BankDeposit,
)
from app.services.totals import month_official_total, month_official_fund_total, month_bounds
from app.services.banking import awaiting_banking, deposited_fund_total_all_time

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
@login_required
def dashboard():
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month

    mukululo = month_official_total(year, month, CollectionType.MUKULULO)
    friday = month_official_total(year, month, CollectionType.FRIDAY)
    sunday = month_official_total(year, month, CollectionType.SUNDAY)
    friday_sunday_combined = friday + sunday
    overall = mukululo + friday_sunday_combined

    start, end = month_bounds(year, month)
    mukululo_banked = _month_deposited(FundType.MUKULULO, start, end)
    fs_banked = _month_deposited(FundType.FRIDAY_SUNDAY, start, end)

    cards = {
        "mukululo": {
            "collected": mukululo,
            "banked": mukululo_banked,
            "awaiting": mukululo - mukululo_banked,
        },
        "friday": {"collected": friday},
        "sunday": {"collected": sunday},
        "friday_sunday": {
            "collected": friday_sunday_combined,
            "banked": fs_banked,
            "awaiting": friday_sunday_combined - fs_banked,
        },
        "overall": {
            "collected": overall,
            "banked": mukululo_banked + fs_banked,
            "awaiting": overall - (mukululo_banked + fs_banked),
        },
    }

    recent_transactions = (
        ContributionTransaction.query.filter(ContributionTransaction.status == TransactionStatus.ACTIVE)
        .order_by(ContributionTransaction.created_at.desc())
        .limit(10)
        .all()
    )
    recent_deposits = BankDeposit.query.order_by(BankDeposit.created_at.desc()).limit(10).all()

    overall_awaiting_all_time = awaiting_banking(FundType.MUKULULO) + awaiting_banking(FundType.FRIDAY_SUNDAY)

    return render_template(
        "main/dashboard.html",
        year=year,
        month=month,
        cards=cards,
        recent_transactions=recent_transactions,
        recent_deposits=recent_deposits,
        overall_awaiting_all_time=overall_awaiting_all_time,
        today=today,
    )


def _month_deposited(fund, start, end):
    from sqlalchemy import func
    total = (
        BankDeposit.query.filter(
            BankDeposit.fund == fund,
            BankDeposit.deposit_date >= start,
            BankDeposit.deposit_date <= end,
        )
        .with_entities(func.coalesce(func.sum(BankDeposit.amount), 0))
        .scalar()
    )
    return int(total or 0)
