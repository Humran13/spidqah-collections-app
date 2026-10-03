from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, current_app
from flask_login import login_required

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    CollectionType,
    TransactionStatus,
    BankDeposit,
    BankReconciliation,
    FundType,
)
from app.services.totals import (
    contributor_detail_total,
    month_official_total,
    month_official_fund_total,
    month_bounds,
    chikumi_100_totals,
)
from app.services.banking import awaiting_banking
from app.services.reports import (
    annual_contributor_ranking,
    annual_contributor_monthly_breakdown,
    contributor_report_rows,
    daily_category_rows,
)
from app.services.report_filters import range_from_args, export_url, resolve_period
from app.services.daily_reconciliation import daily_reconciliation_rows, summarize
from app.utils import roles_required, to_local
from app.models import UserRole
from app.services.pdf import generate_statement_pdf, generate_batch_statements_pdf
from app.blueprints.reports.csv_utils import csv_response

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _range_from_request():
    """Shared filter parsing: Exact Date wins over From/To. See
    app.services.report_filters."""
    return range_from_args(request.args)


@reports_bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@reports_bp.route("/contributor")
@login_required
def contributor_report():
    rng = _range_from_request()
    rows, totals = contributor_report_rows(rng.start, rng.end)

    if request.args.get("format") == "csv":
        body = [[r["contributor"].name, r["mukululo"], r["friday"], r["sunday"], r["total"]] for r in rows]
        body.append(["GRAND TOTAL", totals["MUKULULO"], totals["FRIDAY"], totals["SUNDAY"], totals["GRAND"]])
        return csv_response(
            "contributor_report.csv",
            ["Contributor", "Mukululo", "Friday", "Sunday", "Total"],
            body,
        )
    return render_template(
        "reports/contributor_report.html", rows=rows, totals=totals, rng=rng,
        start=rng.start, end=rng.end,
        csv_url=export_url("reports.contributor_report", rng),
    )


@reports_bp.route("/collection/<fund_key>")
@login_required
def collection_report(fund_key):
    mapping = {
        "mukululo": ("Mukululo", [CollectionType.MUKULULO]),
        "friday": ("Friday", [CollectionType.FRIDAY]),
        "sunday": ("Sunday", [CollectionType.SUNDAY]),
        "friday_sunday": ("Friday & Sunday Combined", [CollectionType.FRIDAY, CollectionType.SUNDAY]),
    }
    if fund_key not in mapping:
        flash("Unknown report.", "danger")
        return redirect(url_for("reports.index"))
    title, types = mapping[fund_key]
    rng = _range_from_request()
    start, end = rng.start, rng.end

    q = ContributionTransaction.query.filter(
        ContributionTransaction.status == TransactionStatus.ACTIVE,
        ContributionTransaction.collection_type.in_(types),
        ContributionTransaction.date >= start,
        ContributionTransaction.date <= end,
    ).order_by(ContributionTransaction.date.asc())
    transactions = q.all()

    daily = {}
    for t in transactions:
        daily.setdefault(t.date, 0)
        daily[t.date] += t.amount
    daily_rows = sorted(daily.items())
    total = sum(daily.values())

    if request.args.get("format") == "csv":
        return csv_response(
            f"{fund_key}_report.csv",
            ["Date", "Total"],
            [[d.isoformat(), amt] for d, amt in daily_rows],
        )

    return render_template(
        "reports/collection_report.html", title=title, fund_key=fund_key,
        daily_rows=daily_rows, total=total, start=start, end=end, rng=rng,
        csv_url=export_url("reports.collection_report", rng, fund_key=fund_key),
    )


@reports_bp.route("/combined")
@login_required
def combined_report():
    rng = _range_from_request()
    rows, totals = daily_category_rows(rng.start, rng.end)

    if request.args.get("format") == "csv":
        body = [[r["date"].isoformat(), r["mukululo"], r["friday"], r["sunday"], r["total"]] for r in rows]
        body.append(["GRAND TOTAL", totals["MUKULULO"], totals["FRIDAY"], totals["SUNDAY"], totals["GRAND"]])
        return csv_response(
            "combined_report.csv",
            ["Date", "Mukululo", "Friday", "Sunday", "Total"],
            body,
        )
    return render_template(
        "reports/combined_report.html", rows=rows, totals=totals, rng=rng,
        start=rng.start, end=rng.end,
        csv_url=export_url("reports.combined_report", rng),
    )


DAILY_RECON_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY)


def _period_label(rng, filter_args):
    if rng.is_exact:
        return f"Exact date: {rng.start.strftime('%d %b %Y')}"
    if "date_from" in filter_args:
        return f"{rng.start.strftime('%d %b %Y')} to {rng.end.strftime('%d %b %Y')}"
    if "month" in filter_args:
        return rng.start.strftime("%B %Y")
    return f"Year {rng.start.year}"


@reports_bp.route("/daily-reconciliation")
@login_required
@roles_required(*DAILY_RECON_ROLES)
def daily_reconciliation():
    """Physical cash closings over time. Read-only: reads the existing Daily
    Close records; never changes them. Same visibility as the Daily Close
    screen, because it shows physical counts and variances."""
    rng, filter_args = resolve_period(request.args)
    rows = daily_reconciliation_rows(rng.start, rng.end)
    summary = summarize(rows)
    period = _period_label(rng, filter_args)

    if request.args.get("format") == "csv":
        body = [[
            r["date"].isoformat(), r["mukululo"], r["friday"], r["sunday"], r["expected"],
            r["physical"], r["difference"], r["status"], r["closed_by"],
            r["closed_at"].strftime("%Y-%m-%d %H:%M") if r["closed_at"] else "", r["notes"],
        ] for r in rows]
        body.append([
            "GRAND TOTAL", summary["mukululo"], summary["friday"], summary["sunday"],
            summary["expected"], summary["physical"], summary["net_difference"], "", "", "", "",
        ])
        return csv_response(
            "daily_reconciliation.csv",
            ["Collection Date", "Mukululo", "Friday", "Sunday", "Expected Cash", "Physical Count",
             "Difference", "Status", "Closed By", "Closed At", "Notes"],
            body,
        )

    if request.args.get("format") == "print":
        return render_template("reports/daily_reconciliation_print.html", rows=rows, summary=summary,
                               period=period, org_name=current_app.config["ORG_NAME"],
                               now_label=to_local(datetime.utcnow()).strftime("%d %b %Y, %I:%M %p"))

    return render_template(
        "reports/daily_reconciliation.html", rows=rows, summary=summary, rng=rng,
        filter_args=filter_args, period=period, show_month_year=True,
        csv_url=url_for("reports.daily_reconciliation", format="csv", **filter_args),
        print_url=url_for("reports.daily_reconciliation", format="print", **filter_args),
    )


@reports_bp.route("/chikumi100")
@login_required
def chikumi100_report():
    rng = _range_from_request()
    start, end = rng.start, rng.end
    contributor = Contributor.query.filter_by(name_normalized="CHIKUMI 100").first()
    total = chikumi_100_totals(start, end)

    monthly = []
    if start and end:
        y = start.year
        while y <= end.year:
            for m in range(1, 13):
                m_start, m_end = month_bounds(y, m)
                if m_end < start or m_start > end:
                    continue
                amt = chikumi_100_totals(max(m_start, start), min(m_end, end))
                if amt:
                    monthly.append({"year": y, "month": m, "total": amt})
            y += 1

    return render_template(
        "reports/chikumi100_report.html", total=total, monthly=monthly,
        start=start, end=end, contributor=contributor, rng=rng,
        csv_url=None,
    )


@reports_bp.route("/monthly")
@login_required
def monthly_collection_report():
    year = request.args.get("year", type=int) or date.today().year
    rows = []
    for m in range(1, 13):
        mukululo = month_official_total(year, m, CollectionType.MUKULULO)
        friday = month_official_total(year, m, CollectionType.FRIDAY)
        sunday = month_official_total(year, m, CollectionType.SUNDAY)
        rows.append({"month": m, "mukululo": mukululo, "friday": friday, "sunday": sunday, "total": mukululo + friday + sunday})

    if request.args.get("format") == "csv":
        return csv_response(
            f"monthly_collection_report_{year}.csv",
            ["Month", "Mukululo", "Friday", "Sunday", "Total"],
            [[r["month"], r["mukululo"], r["friday"], r["sunday"], r["total"]] for r in rows],
        )
    return render_template("reports/monthly_report.html", year=year, rows=rows)


@reports_bp.route("/banking")
@login_required
def banking_report():
    rng = _range_from_request()
    start, end = rng.start, rng.end
    deposits = BankDeposit.query.filter(
        BankDeposit.deposit_date >= start, BankDeposit.deposit_date <= end
    ).order_by(BankDeposit.deposit_date.asc()).all()
    total = sum(d.amount for d in deposits)

    if request.args.get("format") == "csv":
        return csv_response(
            "banking_report.csv",
            ["Date", "Fund", "Bank Account", "Amount", "Reference", "Slip Ref"],
            [[d.deposit_date.isoformat(), d.fund.value, d.bank_account.name, d.amount, d.reference or "", d.slip_reference or ""] for d in deposits],
        )
    return render_template(
        "reports/banking_report.html", deposits=deposits, total=total, start=start, end=end, rng=rng,
        csv_url=export_url("reports.banking_report", rng),
    )


@reports_bp.route("/awaiting-banking")
@login_required
def awaiting_banking_report():
    mukululo_awaiting = awaiting_banking(FundType.MUKULULO)
    fs_awaiting = awaiting_banking(FundType.FRIDAY_SUNDAY)
    return render_template(
        "reports/awaiting_banking_report.html",
        mukululo_awaiting=mukululo_awaiting, fs_awaiting=fs_awaiting,
        total_awaiting=mukululo_awaiting + fs_awaiting,
    )


@reports_bp.route("/bank-reconciliation")
@login_required
def bank_reconciliation_report():
    reconciliations = BankReconciliation.query.order_by(BankReconciliation.balance_date.desc()).limit(200).all()
    return render_template("reports/bank_reconciliation_report.html", reconciliations=reconciliations)


@reports_bp.route("/annual-summary")
@login_required
def annual_summary():
    year = request.args.get("year", type=int) or date.today().year
    rows = []
    totals = {"mukululo": 0, "friday": 0, "sunday": 0, "total": 0}
    for m in range(1, 13):
        mukululo = month_official_total(year, m, CollectionType.MUKULULO)
        friday = month_official_total(year, m, CollectionType.FRIDAY)
        sunday = month_official_total(year, m, CollectionType.SUNDAY)
        rows.append({"month": m, "mukululo": mukululo, "friday": friday, "sunday": sunday, "total": mukululo + friday + sunday})
        totals["mukululo"] += mukululo
        totals["friday"] += friday
        totals["sunday"] += sunday
        totals["total"] += mukululo + friday + sunday

    start, end = date(year, 1, 1), date(year, 12, 31)
    deposited = BankDeposit.query.filter(BankDeposit.deposit_date >= start, BankDeposit.deposit_date <= end)
    deposited_total = sum(d.amount for d in deposited.all())

    return render_template("reports/annual_summary.html", year=year, rows=rows, totals=totals, deposited_total=deposited_total)


@reports_bp.route("/ranking")
@login_required
def ranking():
    year = request.args.get("year", type=int) or date.today().year
    fund_filter = request.args.get("fund", "ALL")
    include_zero = request.args.get("include_zero", "1") == "1"
    results = annual_contributor_ranking(year, fund_filter, include_zero)

    if request.args.get("format") == "csv":
        return csv_response(
            f"ranking_{year}.csv",
            ["Rank", "Contributor", "Total"],
            [[i + 1, r["contributor"].name, r["total"]] for i, r in enumerate(results)],
        )
    return render_template("reports/ranking.html", year=year, fund_filter=fund_filter, include_zero=include_zero, results=results)


@reports_bp.route("/statement")
@login_required
def statement():
    year = request.args.get("year", type=int) or date.today().year
    contributor_id = request.args.get("contributor_id", type=int)
    contributors = Contributor.query.filter(Contributor.merged_into_id.is_(None)).order_by(Contributor.name).all()

    contributor = None
    rows = None
    if contributor_id:
        contributor = db.session.get(Contributor, contributor_id)
        if contributor:
            rows = annual_contributor_monthly_breakdown(contributor.id, year)

    return render_template("reports/statement.html", year=year, contributors=contributors, contributor=contributor, rows=rows)


@reports_bp.route("/statement/pdf")
@login_required
def statement_pdf():
    year = request.args.get("year", type=int) or date.today().year
    contributor_id = request.args.get("contributor_id", type=int)
    contributor = db.session.get(Contributor, contributor_id) if contributor_id else None
    if not contributor:
        flash("Select a contributor first.", "danger")
        return redirect(url_for("reports.statement", year=year))

    pdf_bytes = generate_statement_pdf(current_app.config["ORG_NAME"], contributor, year)
    filename = f"statement_{contributor.name.replace(' ', '_')}_{year}.pdf"
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})


@reports_bp.route("/statement/batch-pdf")
@login_required
def statement_batch_pdf():
    year = request.args.get("year", type=int) or date.today().year
    contributors = Contributor.query.filter(
        Contributor.merged_into_id.is_(None), Contributor.active.is_(True)
    ).order_by(Contributor.name).all()
    if not contributors:
        flash("No contributors found.", "warning")
        return redirect(url_for("reports.statement", year=year))

    pdf_bytes = generate_batch_statements_pdf(current_app.config["ORG_NAME"], contributors, year)
    filename = f"statements_batch_{year}.pdf"
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})
