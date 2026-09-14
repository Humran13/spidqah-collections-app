import secrets
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    User,
    UserRole,
    HistoricalOfficialMonthlyTotal,
    AuditLog,
    Contributor,
    ContributionTransaction,
    AuditAction,
)
from app.services.audit import log_audit
from app.services.settings import get_go_live_date, set_go_live_date, set_setting, get_setting, ORG_NAME_KEY
from app.utils import roles_required, parse_amount_ugx_allow_zero
from app.blueprints.admin.forms import UserForm, HistoricalTotalForm, UnlockForm, SettingsForm, MergeContributorForm

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_ONLY = (UserRole.ADMIN,)


@admin_bp.route("/")
@login_required
@roles_required(*ADMIN_ONLY)
def dashboard():
    return render_template("admin/index.html")


# --- Users -----------------------------------------------------------------

@admin_bp.route("/users")
@login_required
@roles_required(*ADMIN_ONLY)
def users_list():
    users = User.query.order_by(User.username.asc()).all()
    return render_template("admin/users_list.html", users=users)


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def new_user():
    form = UserForm()
    if form.validate_on_submit():
        if User.query.filter_by(username=form.username.data.strip()).first():
            flash("Username already exists.", "danger")
            return render_template("admin/user_form.html", form=form, user=None)

        password = form.password.data or secrets.token_urlsafe(12)
        user = User(
            username=form.username.data.strip(),
            display_name=form.display_name.data.strip(),
            email=(form.email.data or "").strip() or None,
            role=UserRole[form.role.data],
            active=form.active.data,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        log_audit("User", user.id, AuditAction.CREATE, after={"username": user.username, "role": user.role.value})
        db.session.commit()
        if not form.password.data:
            flash(f"User created. Temporary password: {password} (share securely, ask them to change it).", "success")
        else:
            flash("User created.", "success")
        return redirect(url_for("admin.users_list"))

    return render_template("admin/user_form.html", form=form, user=None)


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users_list"))

    form = UserForm(obj=user)
    if request.method == "GET":
        form.role.data = user.role.value
        form.password.data = ""

    if form.validate_on_submit():
        if user.role == UserRole.ADMIN and UserRole[form.role.data] != UserRole.ADMIN:
            remaining_admins = User.query.filter(User.role == UserRole.ADMIN, User.id != user.id, User.active.is_(True)).count()
            if remaining_admins == 0:
                flash("Cannot remove the last active Admin.", "danger")
                return render_template("admin/user_form.html", form=form, user=user)

        before = {"role": user.role.value, "active": user.active}
        user.username = form.username.data.strip()
        user.display_name = form.display_name.data.strip()
        user.email = (form.email.data or "").strip() or None
        user.role = UserRole[form.role.data]
        user.active = form.active.data
        if form.password.data:
            user.set_password(form.password.data)
            log_audit("User", user.id, AuditAction.RESET_PASSWORD, reason="Admin reset password")
        after = {"role": user.role.value, "active": user.active}
        log_audit("User", user.id, AuditAction.EDIT, before=before, after=after)
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.users_list"))

    return render_template("admin/user_form.html", form=form, user=user)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def toggle_user_active(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users_list"))
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin.users_list"))

    if user.active and user.role == UserRole.ADMIN:
        remaining_admins = User.query.filter(User.role == UserRole.ADMIN, User.id != user.id, User.active.is_(True)).count()
        if remaining_admins == 0:
            flash("Cannot deactivate the last active Admin.", "danger")
            return redirect(url_for("admin.users_list"))

    user.active = not user.active
    log_audit("User", user.id, AuditAction.ACTIVATE if user.active else AuditAction.DEACTIVATE, user=current_user)
    db.session.commit()
    flash(f"User {'activated' if user.active else 'deactivated'}.", "success")
    return redirect(url_for("admin.users_list"))


# --- Historical Official Totals ---------------------------------------------

@admin_bp.route("/historical-totals")
@login_required
@roles_required(*ADMIN_ONLY)
def historical_totals_list():
    totals = HistoricalOfficialMonthlyTotal.query.order_by(
        HistoricalOfficialMonthlyTotal.year.desc(), HistoricalOfficialMonthlyTotal.month.desc()
    ).all()
    return render_template("admin/historical_totals_list.html", totals=totals)


@admin_bp.route("/historical-totals/new", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def new_historical_total():
    form = HistoricalTotalForm()
    if form.validate_on_submit():
        existing = HistoricalOfficialMonthlyTotal.query.filter_by(year=form.year.data, month=form.month.data).first()
        if existing:
            flash("A total for that year/month already exists. Edit it instead.", "warning")
            return redirect(url_for("admin.edit_historical_total", total_id=existing.id))

        try:
            mukululo = parse_amount_ugx_allow_zero(form.mukululo_total.data)
            friday = parse_amount_ugx_allow_zero(form.friday_total.data)
            sunday = parse_amount_ugx_allow_zero(form.sunday_total.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("admin/historical_total_form.html", form=form, total=None)

        row = HistoricalOfficialMonthlyTotal(
            year=form.year.data, month=form.month.data,
            mukululo_total=mukululo, friday_total=friday, sunday_total=sunday,
            notes=(form.notes.data or "").strip() or None,
            locked=False,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.flush()
        log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.CREATE, after={
            "year": row.year, "month": row.month, "mukululo": mukululo, "friday": friday, "sunday": sunday,
        })
        db.session.commit()
        flash("Historical official total saved.", "success")
        return redirect(url_for("admin.historical_totals_list"))

    return render_template("admin/historical_total_form.html", form=form, total=None)


@admin_bp.route("/historical-totals/<int:total_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def edit_historical_total(total_id):
    row = db.session.get(HistoricalOfficialMonthlyTotal, total_id)
    if not row:
        flash("Not found.", "danger")
        return redirect(url_for("admin.historical_totals_list"))

    if row.locked:
        flash("This total is locked. Unlock it first to make changes.", "warning")
        return redirect(url_for("admin.historical_totals_list"))

    form = HistoricalTotalForm(obj=row)
    if request.method == "GET":
        form.mukululo_total.data = str(row.mukululo_total)
        form.friday_total.data = str(row.friday_total)
        form.sunday_total.data = str(row.sunday_total)

    if form.validate_on_submit():
        try:
            mukululo = parse_amount_ugx_allow_zero(form.mukululo_total.data)
            friday = parse_amount_ugx_allow_zero(form.friday_total.data)
            sunday = parse_amount_ugx_allow_zero(form.sunday_total.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("admin/historical_total_form.html", form=form, total=row)

        before = {"mukululo": row.mukululo_total, "friday": row.friday_total, "sunday": row.sunday_total}
        row.year = form.year.data
        row.month = form.month.data
        row.mukululo_total = mukululo
        row.friday_total = friday
        row.sunday_total = sunday
        row.notes = (form.notes.data or "").strip() or None
        row.last_modified_by_id = current_user.id
        row.last_modified_at = datetime.utcnow()
        after = {"mukululo": mukululo, "friday": friday, "sunday": sunday}
        log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.EDIT, before=before, after=after)
        db.session.commit()
        flash("Historical official total updated.", "success")
        return redirect(url_for("admin.historical_totals_list"))

    return render_template("admin/historical_total_form.html", form=form, total=row)


@admin_bp.route("/historical-totals/<int:total_id>/lock", methods=["POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def lock_historical_total(total_id):
    row = db.session.get(HistoricalOfficialMonthlyTotal, total_id)
    if row:
        row.locked = True
        log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.LOCK)
        db.session.commit()
        flash(f"{row.year}-{row.month:02d} locked.", "success")
    return redirect(url_for("admin.historical_totals_list"))


@admin_bp.route("/historical-totals/<int:total_id>/unlock", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def unlock_historical_total(total_id):
    row = db.session.get(HistoricalOfficialMonthlyTotal, total_id)
    if not row:
        flash("Not found.", "danger")
        return redirect(url_for("admin.historical_totals_list"))

    form = UnlockForm()
    if form.validate_on_submit():
        row.locked = False
        log_audit("HistoricalOfficialMonthlyTotal", row.id, AuditAction.UNLOCK, reason=form.reason.data)
        db.session.commit()
        flash(f"{row.year}-{row.month:02d} unlocked.", "success")
        return redirect(url_for("admin.historical_totals_list"))

    return render_template("admin/unlock_form.html", form=form, total=row)


# --- Settings ----------------------------------------------------------------

@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def settings():
    form = SettingsForm()
    if request.method == "GET":
        form.org_name.data = get_setting(ORG_NAME_KEY, "SPIDQAH")
        form.go_live_date.data = get_go_live_date()

    if form.validate_on_submit():
        before = {"go_live_date": get_go_live_date().isoformat(), "org_name": get_setting(ORG_NAME_KEY)}
        set_setting(ORG_NAME_KEY, form.org_name.data.strip(), user_id=current_user.id)
        set_go_live_date(form.go_live_date.data, user_id=current_user.id)
        after = {"go_live_date": form.go_live_date.data.isoformat(), "org_name": form.org_name.data.strip()}
        log_audit("AppSetting", None, AuditAction.EDIT, before=before, after=after)
        db.session.commit()
        flash("Settings updated.", "success")
        return redirect(url_for("admin.settings"))

    return render_template("admin/settings.html", form=form)


# --- Audit log -----------------------------------------------------------------

@admin_bp.route("/audit-log")
@login_required
@roles_required(*ADMIN_ONLY)
def audit_log():
    entity_type = request.args.get("entity_type")
    query = AuditLog.query
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    entries = query.order_by(AuditLog.timestamp.desc()).limit(300).all()
    entity_types = [row[0] for row in db.session.query(AuditLog.entity_type).distinct().all()]
    return render_template("admin/audit_log.html", entries=entries, entity_types=entity_types, selected_type=entity_type)


# --- Contributor merge -----------------------------------------------------

@admin_bp.route("/contributors/merge", methods=["GET", "POST"])
@login_required
@roles_required(*ADMIN_ONLY)
def merge_contributors():
    contributors = Contributor.query.filter(Contributor.merged_into_id.is_(None)).order_by(Contributor.name).all()
    form = MergeContributorForm()
    form.source_id.choices = [(c.id, c.name) for c in contributors]
    form.target_id.choices = [(c.id, c.name) for c in contributors]

    if form.validate_on_submit():
        if form.source_id.data == form.target_id.data:
            flash("Choose two different contributors.", "danger")
            return render_template("admin/merge_contributors.html", form=form)

        source = db.session.get(Contributor, form.source_id.data)
        target = db.session.get(Contributor, form.target_id.data)

        moved = ContributionTransaction.query.filter_by(contributor_id=source.id).update(
            {"contributor_id": target.id}, synchronize_session=False
        )
        source.merged_into_id = target.id
        source.active = False
        log_audit("Contributor", source.id, AuditAction.MERGE, before={"name": source.name},
                   after={"merged_into": target.name, "transactions_moved": moved})
        db.session.commit()
        flash(f'"{source.name}" merged into "{target.name}" ({moved} transaction(s) moved). Transaction history preserved.', "success")
        return redirect(url_for("contributors.profile", contributor_id=target.id))

    return render_template("admin/merge_contributors.html", form=form)
