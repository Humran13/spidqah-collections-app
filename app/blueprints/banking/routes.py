from datetime import date, datetime
from calendar import month_name

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    BankAccount,
    BankDeposit,
    BankAdjustment,
    BankReconciliation,
    FundType,
    UserRole,
    AuditAction,
    ReconciliationStatus,
    HistoricalCashMovement,
    TransactionStatus,
)
from app.services.banking import awaiting_banking, estimated_balance
from app.services.totals import month_official_fund_total
from app.services.pooled_cash import (
    month_gross_pooled, month_historical_issued, month_pooled_deposited, month_adjusted_remaining,
    overall_awaiting_banking, pooled_historical_issued_all_time,
)
from app.services.audit import log_audit
from app.utils import roles_required, parse_amount_ugx, parse_amount_ugx_allow_zero
from app.blueprints.banking.forms import DepositForm, AdjustmentForm, ReconciliationForm, BankAccountForm

banking_bp = Blueprint("banking", __name__, url_prefix="/banking")

ENTRY_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY)
ADMIN_ONLY = (UserRole.ADMIN,)


def _accounts():
    return BankAccount.query.order_by(BankAccount.name.asc()).all()


@banking_bp.route("/")
@login_required
def overview():
    accounts = _accounts()
    account_rows = []
    for acc in accounts:
        account_rows.append({
            "account": acc,
            "estimated_balance": estimated_balance(acc),
        })
    fund_awaiting = {
        "MUKULULO": awaiting_banking(FundType.MUKULULO),
        "FRIDAY_SUNDAY": awaiting_banking(FundType.FRIDAY_SUNDAY),
    }
    return render_template("banking/overview.html", account_rows=account_rows, fund_awaiting=fund_awaiting)


@banking_bp.route("/deposits")
@login_required
def deposits_list():
    deposits = BankDeposit.query.order_by(BankDeposit.deposit_date.desc(), BankDeposit.created_at.desc()).limit(200).all()
    return render_template("banking/deposits_list.html", deposits=deposits)


@banking_bp.route("/deposits/new", methods=["GET", "POST"])
@login_required
@roles_required(*ENTRY_ROLES)
def new_deposit():
    form = DepositForm()
    form.bank_account_id.choices = [(a.id, a.name) for a in _accounts()]

    if form.validate_on_submit():
        try:
            amount = parse_amount_ugx(form.amount.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("banking/deposit_form.html", form=form)

        fund = FundType[form.fund.data]
        available = awaiting_banking(fund, upto=form.deposit_date.data)
        exceeds = amount > available

        if exceeds and not form.override.data:
            flash(
                f"This deposit (UGX {amount:,}) exceeds the UGX {available:,} currently awaiting banking for "
                f"{fund.value.replace('_', ' & ').title()}. Check the amount, or tick override with a reason if this is intentional.",
                "danger",
            )
            return render_template("banking/deposit_form.html", form=form, exceeds=True, available=available)

        deposit = BankDeposit(
            deposit_date=form.deposit_date.data,
            fund=fund,
            bank_account_id=form.bank_account_id.data,
            amount=amount,
            reference=(form.reference.data or "").strip() or None,
            slip_reference=(form.slip_reference.data or "").strip() or None,
            notes=(form.notes.data or "").strip() or None,
            exceeds_available=exceeds,
            override_reason=(form.override_reason.data or "").strip() or None if exceeds else None,
            created_by_id=current_user.id,
        )
        db.session.add(deposit)
        db.session.flush()
        log_audit("BankDeposit", deposit.id, AuditAction.CREATE, after={
            "date": deposit.deposit_date.isoformat(), "fund": fund.value, "amount": amount,
            "bank_account_id": deposit.bank_account_id, "override": exceeds,
        }, reason=deposit.override_reason)
        db.session.commit()
        flash("Deposit recorded.", "success")
        return redirect(url_for("banking.deposits_list"))

    return render_template("banking/deposit_form.html", form=form)


@banking_bp.route("/adjustments/new", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def new_adjustment():
    form = AdjustmentForm()
    form.bank_account_id.choices = [(a.id, a.name) for a in _accounts()]

    if form.validate_on_submit():
        try:
            amount = parse_amount_ugx(form.amount.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("banking/adjustment_form.html", form=form)

        from app.models import AdjustmentDirection
        adjustment = BankAdjustment(
            bank_account_id=form.bank_account_id.data,
            date=form.date.data,
            amount=amount,
            direction=AdjustmentDirection[form.direction.data],
            reason=form.reason.data,
            created_by_id=current_user.id,
        )
        db.session.add(adjustment)
        db.session.flush()
        log_audit("BankAdjustment", adjustment.id, AuditAction.CREATE, after={
            "bank_account_id": adjustment.bank_account_id, "amount": amount,
            "direction": adjustment.direction.value,
        }, reason=adjustment.reason)
        db.session.commit()
        flash("Adjustment recorded.", "success")
        return redirect(url_for("banking.overview"))

    return render_template("banking/adjustment_form.html", form=form)


@banking_bp.route("/reconciliation")
@login_required
def reconciliation_list():
    reconciliations = BankReconciliation.query.order_by(BankReconciliation.balance_date.desc()).limit(100).all()
    return render_template("banking/reconciliation_list.html", reconciliations=reconciliations)


@banking_bp.route("/reconciliation/new", methods=["GET", "POST"])
@login_required
@roles_required(*ENTRY_ROLES)
def new_reconciliation():
    form = ReconciliationForm()
    form.bank_account_id.choices = [(a.id, a.name) for a in _accounts()]

    if form.validate_on_submit():
        try:
            actual = parse_amount_ugx(form.actual_balance.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("banking/reconciliation_form.html", form=form)

        account = db.session.get(BankAccount, form.bank_account_id.data)
        estimate = estimated_balance(account, upto=form.balance_date.data)
        difference = actual - estimate
        status = ReconciliationStatus.RECONCILED if difference == 0 else ReconciliationStatus.DIFFERENCE

        recon = BankReconciliation(
            bank_account_id=account.id,
            balance_date=form.balance_date.data,
            actual_balance=actual,
            estimated_balance_snapshot=estimate,
            difference=difference,
            status=status,
            note=(form.note.data or "").strip() or None,
            created_by_id=current_user.id,
        )
        db.session.add(recon)
        db.session.flush()
        log_audit("BankReconciliation", recon.id, AuditAction.CREATE, after={
            "bank_account_id": account.id, "actual": actual, "estimate": estimate,
            "difference": difference, "status": status.value,
        })
        db.session.commit()
        flash(f"Reconciliation saved: {status.value}.", "success")
        return redirect(url_for("banking.reconciliation_list"))

    return render_template("banking/reconciliation_form.html", form=form)


@banking_bp.route("/monthly")
@login_required
def monthly_view():
    today = date.today()
    year = request.args.get("year", type=int) or today.year

    import calendar

    rows = []
    for m in range(1, 13):
        month_start = date(year, m, 1)
        month_end = date(year, m, calendar.monthrange(year, m)[1])

        mukululo_collected = month_official_fund_total(year, m, FundType.MUKULULO)
        fs_collected = month_official_fund_total(year, m, FundType.FRIDAY_SUNDAY)

        mukululo_deposited = _month_fund_deposits(FundType.MUKULULO, month_start, month_end)
        fs_deposited = _month_fund_deposits(FundType.FRIDAY_SUNDAY, month_start, month_end)

        gross_pooled = month_gross_pooled(year, m)
        historical_issued = month_historical_issued(year, m)
        adjusted_remaining = month_adjusted_remaining(year, m)

        rows.append({
            "month": m,
            "month_name": month_name[m],
            "mukululo_collected": mukululo_collected,
            "mukululo_deposited": mukululo_deposited,
            "mukululo_remaining": mukululo_collected - mukululo_deposited,
            "fs_collected": fs_collected,
            "fs_deposited": fs_deposited,
            "fs_remaining": fs_collected - fs_deposited,
            "total_collected": mukululo_collected + fs_collected,
            "total_deposited": mukululo_deposited + fs_deposited,
            "total_remaining": (mukululo_collected + fs_collected) - (mukululo_deposited + fs_deposited),
            "gross_pooled": gross_pooled,
            "historical_issued": historical_issued,
            "adjusted_remaining": adjusted_remaining,
        })

    # Also show December of the prior year, since it can carry brought-
    # forward pooled cash into January but would otherwise never appear
    # on a page scoped to a single calendar year.
    dec_prev = None
    prev_gross = month_gross_pooled(year - 1, 12)
    if prev_gross or month_historical_issued(year - 1, 12):
        dec_prev = {
            "month": 12, "month_name": f"December {year - 1}",
            "gross_pooled": prev_gross,
            "historical_issued": month_historical_issued(year - 1, 12),
            "deposited": month_pooled_deposited(year - 1, 12),
            "adjusted_remaining": month_adjusted_remaining(year - 1, 12),
        }

    overall_awaiting = overall_awaiting_banking()
    total_historical_issued = pooled_historical_issued_all_time()

    issued_movements = HistoricalCashMovement.query.filter(
        HistoricalCashMovement.status == TransactionStatus.ACTIVE
    ).order_by(HistoricalCashMovement.movement_date.asc()).all()

    return render_template(
        "banking/monthly.html", year=year, rows=rows, dec_prev=dec_prev,
        overall_awaiting=overall_awaiting, total_historical_issued=total_historical_issued,
        issued_movements=issued_movements,
    )


def _month_fund_deposits(fund, start, end):
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


@banking_bp.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def edit_account(account_id):
    account = db.session.get(BankAccount, account_id)
    if not account:
        flash("Bank account not found.", "danger")
        return redirect(url_for("banking.overview"))

    form = BankAccountForm(obj=account)
    if request.method == "GET":
        form.opening_balance.data = str(account.opening_balance)

    if form.validate_on_submit():
        try:
            opening = parse_amount_ugx_allow_zero(form.opening_balance.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("banking/account_form.html", form=form, account=account)
        before = {"opening_balance": account.opening_balance, "opening_balance_date": str(account.opening_balance_date)}
        account.name = form.name.data.strip()
        account.opening_balance = opening
        account.opening_balance_date = form.opening_balance_date.data
        account.notes = (form.notes.data or "").strip() or None
        after = {"opening_balance": account.opening_balance, "opening_balance_date": str(account.opening_balance_date)}
        log_audit("BankAccount", account.id, AuditAction.EDIT, before=before, after=after)
        db.session.commit()
        flash("Bank account updated.", "success")
        return redirect(url_for("banking.overview"))

    return render_template("banking/account_form.html", form=form, account=account)
