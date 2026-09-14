from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    CollectionType,
    TransactionStatus,
    UserRole,
    AuditAction,
)
from app.services.totals import contributor_detail_total
from app.services.contributors import find_exact, find_similar
from app.services.audit import log_audit
from app.utils import roles_required
from app.blueprints.contributors.forms import ContributorForm

contributors_bp = Blueprint("contributors", __name__, url_prefix="/contributors")

MANAGE_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY)


@contributors_bp.route("/")
@login_required
def list_contributors():
    q = request.args.get("q", "").strip()
    show_inactive = request.args.get("show_inactive") == "1"
    query = Contributor.query.filter(Contributor.merged_into_id.is_(None))
    if not show_inactive:
        query = query.filter(Contributor.active.is_(True))
    if q:
        query = query.filter(Contributor.name_normalized.ilike(f"%{Contributor.normalize(q)}%"))
    contributors = query.order_by(Contributor.name.asc()).all()
    return render_template("contributors/list.html", contributors=contributors, q=q, show_inactive=show_inactive)


@contributors_bp.route("/new", methods=["GET", "POST"])
@login_required
@roles_required(*MANAGE_ROLES)
def new_contributor():
    form = ContributorForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        existing = find_exact(name)
        if existing:
            flash(f'"{existing.name}" already exists.', "warning")
            return redirect(url_for("contributors.profile", contributor_id=existing.id))

        similar = find_similar(name)
        if similar and request.form.get("confirm_new") != "1":
            return render_template("contributors/new.html", form=form, similar=similar)

        contributor = Contributor(
            name=name,
            name_normalized=Contributor.normalize(name),
            phone=(form.phone.data or "").strip() or None,
            notes=(form.notes.data or "").strip() or None,
            active=True,
            created_by_id=current_user.id,
        )
        db.session.add(contributor)
        db.session.commit()
        flash(f"Contributor {contributor.name} added.", "success")
        return redirect(url_for("contributors.profile", contributor_id=contributor.id))

    return render_template("contributors/new.html", form=form, similar=None)


@contributors_bp.route("/<int:contributor_id>")
@login_required
def profile(contributor_id):
    contributor = db.session.get(Contributor, contributor_id)
    if not contributor:
        flash("Contributor not found.", "danger")
        return redirect(url_for("contributors.list_contributors"))

    year = request.args.get("year", type=int) or date.today().year
    start, end = date(year, 1, 1), date(year, 12, 31)

    mukululo_total = contributor_detail_total([CollectionType.MUKULULO], start, end, contributor.id)
    friday_total = contributor_detail_total([CollectionType.FRIDAY], start, end, contributor.id)
    sunday_total = contributor_detail_total([CollectionType.SUNDAY], start, end, contributor.id)

    txn_query = ContributionTransaction.query.filter(
        ContributionTransaction.contributor_id == contributor.id,
        ContributionTransaction.status == TransactionStatus.ACTIVE,
        ContributionTransaction.date >= start,
        ContributionTransaction.date <= end,
    )
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if date_from:
        txn_query = txn_query.filter(ContributionTransaction.date >= datetime.strptime(date_from, "%Y-%m-%d").date())
    if date_to:
        txn_query = txn_query.filter(ContributionTransaction.date <= datetime.strptime(date_to, "%Y-%m-%d").date())
    collection_type = request.args.get("type")
    if collection_type in CollectionType.__members__:
        txn_query = txn_query.filter(ContributionTransaction.collection_type == CollectionType[collection_type])

    transactions = txn_query.order_by(ContributionTransaction.date.desc()).all()

    return render_template(
        "contributors/profile.html",
        contributor=contributor,
        year=year,
        mukululo_total=mukululo_total,
        friday_total=friday_total,
        sunday_total=sunday_total,
        overall_total=mukululo_total + friday_total + sunday_total,
        transactions=transactions,
    )


@contributors_bp.route("/<int:contributor_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*MANAGE_ROLES)
def edit_contributor(contributor_id):
    contributor = db.session.get(Contributor, contributor_id)
    if not contributor:
        flash("Contributor not found.", "danger")
        return redirect(url_for("contributors.list_contributors"))

    form = ContributorForm(obj=contributor)
    if form.validate_on_submit():
        before = {"name": contributor.name, "phone": contributor.phone, "active": contributor.active}
        contributor.name = form.name.data.strip()
        contributor.name_normalized = Contributor.normalize(contributor.name)
        contributor.phone = (form.phone.data or "").strip() or None
        contributor.notes = (form.notes.data or "").strip() or None
        contributor.active = form.active.data
        after = {"name": contributor.name, "phone": contributor.phone, "active": contributor.active}
        log_audit("Contributor", contributor.id, AuditAction.EDIT, before=before, after=after)
        db.session.commit()
        flash("Contributor updated.", "success")
        return redirect(url_for("contributors.profile", contributor_id=contributor.id))

    return render_template("contributors/edit.html", form=form, contributor=contributor)
