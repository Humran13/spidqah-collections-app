from datetime import date, datetime

from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    ContributionTransaction,
    Contributor,
    CollectionType,
    TransactionStatus,
    UserRole,
    AuditAction,
)
from app.services.contributors import search_contributors, find_exact, find_similar
from app.services.audit import log_audit
from app.services.settings import get_go_live_date
from app.services.daily_close import day_totals, apply_snapshot, refresh_daily_close
from app.services.contribution_corrections import apply_guarded_change, CorrectionBlocked
from app.utils import roles_required, parse_amount_ugx
from app.blueprints.collections.forms import (
    HistoricalEntryForm,
    EditTransactionForm,
    VoidTransactionForm,
    SessionCloseForm,
)

collections_bp = Blueprint("collections", __name__, url_prefix="/collections")

ENTRY_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY, UserRole.COLLECTOR)
BACKENTRY_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY)
EDIT_ROLES = (UserRole.ADMIN, UserRole.DATA_ENTRY)


@collections_bp.route("/entry")
@login_required
@roles_required(*ENTRY_ROLES)
def entry():
    day = request.args.get("date")
    try:
        selected_date = datetime.strptime(day, "%Y-%m-%d").date() if day else date.today()
    except ValueError:
        selected_date = date.today()
    collection_type = request.args.get("type", "MUKULULO")
    if collection_type not in CollectionType.__members__:
        collection_type = "MUKULULO"

    return render_template(
        "collections/entry.html",
        selected_date=selected_date,
        collection_type=collection_type,
        totals=day_totals(selected_date),
    )


@collections_bp.route("/api/contributor-search")
@login_required
@roles_required(*ENTRY_ROLES, UserRole.VIEWER)
def api_contributor_search():
    term = request.args.get("q", "")
    results = search_contributors(term, limit=10)
    return jsonify([{"id": c.id, "name": c.name} for c in results])


@collections_bp.route("/api/save-entry", methods=["POST"])
@login_required
@roles_required(*ENTRY_ROLES)
def api_save_entry():
    payload = request.get_json(silent=True) or {}

    try:
        entry_date = datetime.strptime(payload.get("date", ""), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid date."}), 400

    collection_type_raw = payload.get("collection_type")
    if collection_type_raw not in CollectionType.__members__:
        return jsonify({"ok": False, "error": "Invalid collection type."}), 400
    collection_type = CollectionType[collection_type_raw]

    try:
        amount = parse_amount_ugx(payload.get("amount"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    note = (payload.get("note") or "").strip()[:500] or None

    contributor_id = payload.get("contributor_id")
    contributor_name = (payload.get("contributor_name") or "").strip()
    confirm_new = bool(payload.get("confirm_new"))
    confirm_use_existing_id = payload.get("confirm_use_existing_id")

    contributor = None
    if contributor_id:
        contributor = db.session.get(Contributor, int(contributor_id))
        if contributor is None or not contributor.active:
            return jsonify({"ok": False, "error": "Selected contributor not found."}), 400
    elif confirm_use_existing_id:
        contributor = db.session.get(Contributor, int(confirm_use_existing_id))
    else:
        if not contributor_name:
            return jsonify({"ok": False, "error": "Contributor name is required."}), 400

        exact = find_exact(contributor_name)
        if exact:
            contributor = exact
        else:
            similar = find_similar(contributor_name)
            if similar and not confirm_new:
                return jsonify({
                    "ok": False,
                    "needs_confirmation": True,
                    "similar": [{"id": c.id, "name": c.name} for c in similar],
                    "message": f'"{similar[0].name}" already exists. Use existing contributor, or confirm adding "{contributor_name}" as new?',
                }), 409
            normalized = Contributor.normalize(contributor_name)
            contributor = Contributor(name=contributor_name, name_normalized=normalized, created_by_id=current_user.id)
            db.session.add(contributor)
            db.session.flush()

    txn = ContributionTransaction(
        date=entry_date,
        collection_type=collection_type,
        contributor_id=contributor.id,
        amount=amount,
        note=note,
        is_historical_backentry=False,
        status=TransactionStatus.ACTIVE,
        created_by_id=current_user.id,
    )
    db.session.add(txn)
    db.session.flush()
    log_audit("ContributionTransaction", txn.id, AuditAction.CREATE, after={
        "date": entry_date, "collection_type": collection_type.value,
        "contributor": contributor.name, "amount": amount, "note": note,
    })
    db.session.commit()

    return jsonify({
        "ok": True,
        "contributor": {"id": contributor.id, "name": contributor.name},
        "totals": day_totals(entry_date),
    })


@collections_bp.route("/history")
@login_required
def history():
    day = request.args.get("date")
    try:
        selected_date = datetime.strptime(day, "%Y-%m-%d").date() if day else date.today()
    except ValueError:
        selected_date = date.today()

    collection_type = request.args.get("type")
    query = ContributionTransaction.query.filter(ContributionTransaction.date == selected_date)
    if collection_type in CollectionType.__members__:
        query = query.filter(ContributionTransaction.collection_type == CollectionType[collection_type])

    transactions = query.order_by(ContributionTransaction.created_at.desc()).all()
    return render_template(
        "collections/history.html",
        selected_date=selected_date,
        transactions=transactions,
        collection_type=collection_type,
        can_edit=current_user.role in EDIT_ROLES,
    )


def _txn_snapshot(txn):
    """The audit-relevant state of one contribution entry."""
    return {
        "date": txn.date.isoformat(),
        "collection_type": txn.collection_type.value,
        "contributor": txn.contributor.name,
        "amount": txn.amount,
        "note": txn.note,
        "status": txn.status.value,
    }


def _active_contributor_by_name(raw_name):
    """Corrections only attach an entry to an EXISTING active contributor.
    They never create contributors as a side effect."""
    normalized = Contributor.normalize(raw_name or "")
    contributor = Contributor.query.filter_by(name_normalized=normalized).first() if normalized else None
    if contributor is None or not contributor.active or contributor.merged_into_id is not None:
        raise ValueError(f'No active contributor named "{(raw_name or "").strip()}". Add the contributor first.')
    return contributor


@collections_bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*EDIT_ROLES)
def edit_transaction(txn_id):
    txn = db.session.get(ContributionTransaction, txn_id) or _abort404()
    if txn.status == TransactionStatus.VOID:
        flash("Deleted entries cannot be edited.", "warning")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    form = EditTransactionForm()
    if request.method == "GET":
        form.contributor_name.data = txn.contributor.name
        form.date.data = txn.date
        form.collection_type.data = txn.collection_type.value
        form.amount.data = str(txn.amount)
        form.note.data = txn.note or ""

    if form.validate_on_submit():
        try:
            new_amount = parse_amount_ugx(form.amount.data)
            new_contributor = _active_contributor_by_name(form.contributor_name.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("collections/edit_transaction.html", form=form, txn=txn)

        new_date = form.date.data
        new_type = CollectionType[form.collection_type.data]
        new_note = (form.note.data or "").strip() or None
        old_day = txn.date
        before = _txn_snapshot(txn)

        def mutate():
            txn.date = new_date
            txn.collection_type = new_type
            txn.contributor_id = new_contributor.id
            txn.amount = new_amount
            txn.note = new_note
            txn.updated_by_id = current_user.id
            txn.updated_at = datetime.utcnow()

        try:
            apply_guarded_change([old_day, new_date], mutate)
        except CorrectionBlocked as exc:
            flash(str(exc), "danger")
            return render_template("collections/edit_transaction.html", form=form, txn=txn)

        after = _txn_snapshot(txn)
        if after == before:
            flash("No changes were made.", "info")
            return redirect(url_for("collections.history", date=txn.date.isoformat()))

        log_audit("ContributionTransaction", txn.id, AuditAction.EDIT, before=before, after=after, reason=form.reason.data)
        for day in {old_day, new_date}:
            refresh_daily_close(day, reason="Recalculated after contribution correction")
        db.session.commit()
        flash("Entry corrected. Totals and reports now reflect the change.", "success")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    return render_template("collections/edit_transaction.html", form=form, txn=txn)


@collections_bp.route("/transactions/<int:txn_id>/void", methods=["GET", "POST"])
@login_required
@roles_required(*EDIT_ROLES)
def void_transaction(txn_id):
    """Delete = soft delete. The row is marked VOID, excluded from every
    total, and kept in the audit log. It is never physically removed."""
    txn = db.session.get(ContributionTransaction, txn_id) or _abort404()
    form = VoidTransactionForm()
    if txn.status == TransactionStatus.VOID:
        flash("This entry is already deleted.", "info")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    if form.validate_on_submit():
        day = txn.date
        before = _txn_snapshot(txn)

        def mutate():
            txn.status = TransactionStatus.VOID
            txn.voided_by_id = current_user.id
            txn.voided_at = datetime.utcnow()
            txn.void_reason = form.reason.data

        try:
            apply_guarded_change([day], mutate)
        except CorrectionBlocked as exc:
            flash(str(exc), "danger")
            return render_template("collections/void_transaction.html", form=form, txn=txn)

        after = _txn_snapshot(txn)
        log_audit("ContributionTransaction", txn.id, AuditAction.VOID, before=before, after=after, reason=form.reason.data)
        refresh_daily_close(day, reason="Recalculated after entry deleted")
        db.session.commit()
        flash("Entry deleted. It no longer counts in any total and remains in the audit log.", "success")
        return redirect(url_for("collections.history", date=day.isoformat()))

    return render_template("collections/void_transaction.html", form=form, txn=txn)


@collections_bp.route("/historical-entry", methods=["GET", "POST"])
@login_required
@roles_required(*BACKENTRY_ROLES)
def historical_entry():
    form = HistoricalEntryForm()
    go_live = get_go_live_date()

    if form.validate_on_submit():
        try:
            amount = parse_amount_ugx(form.amount.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("collections/historical_entry.html", form=form, go_live_date=go_live)

        name = form.contributor_name.data.strip()
        contributor = find_exact(name)
        if not contributor:
            normalized = Contributor.normalize(name)
            contributor = Contributor(name=name, name_normalized=normalized, created_by_id=current_user.id)
            db.session.add(contributor)
            db.session.flush()

        txn = ContributionTransaction(
            date=form.date.data,
            collection_type=CollectionType[form.collection_type.data],
            contributor_id=contributor.id,
            amount=amount,
            note=(form.note.data or "").strip() or None,
            is_historical_backentry=True,
            status=TransactionStatus.ACTIVE,
            created_by_id=current_user.id,
        )
        db.session.add(txn)
        db.session.flush()
        log_audit("ContributionTransaction", txn.id, AuditAction.CREATE, after={
            "date": txn.date.isoformat(), "collection_type": txn.collection_type.value,
            "contributor": contributor.name, "amount": amount, "historical_backentry": True,
        })
        db.session.commit()
        flash(f"Historical entry saved for {contributor.name}. Official historical totals were not changed.", "success")
        return redirect(url_for("collections.historical_entry"))

    return render_template("collections/historical_entry.html", form=form, go_live_date=go_live)


@collections_bp.route("/session", methods=["GET", "POST"])
@login_required
@roles_required(*EDIT_ROLES)
def session_close():
    """Daily Close. ONE reconciliation date (the ``date`` query parameter)
    drives both the system totals on the left and the physical cash count
    on the right. The posted form cannot change the date."""
    from app.models import CollectionSession

    day = request.args.get("date")
    try:
        selected_date = datetime.strptime(day, "%Y-%m-%d").date() if day else date.today()
    except ValueError:
        selected_date = date.today()

    existing = CollectionSession.query.filter_by(date=selected_date).first()
    form = SessionCloseForm()

    if request.method == "POST" and request.form.get("reconciliation_date", "") != selected_date.isoformat():
        flash("The reconciliation date did not match this page. Nothing was saved - please try again.", "danger")
        return redirect(url_for("collections.session_close", date=selected_date.isoformat()))

    if request.method == "GET" and existing:
        form.physical_cash_counted.data = str(existing.physical_cash_counted or "")
        form.notes.data = existing.notes

    totals = day_totals(selected_date)

    if form.validate_on_submit():
        try:
            physical = parse_amount_ugx(form.physical_cash_counted.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("collections/session.html", form=form, selected_date=selected_date,
                                    totals=totals, existing=existing)

        session_row = existing or CollectionSession(date=selected_date, created_by_id=current_user.id)
        apply_snapshot(session_row, totals, physical)
        session_row.notes = (form.notes.data or "").strip() or None
        if not existing:
            db.session.add(session_row)
        db.session.commit()
        flash(f"Daily close saved: {session_row.status.value}.", "success")
        return redirect(url_for("collections.session_close", date=selected_date.isoformat()))

    return render_template("collections/session.html", form=form, selected_date=selected_date,
                            totals=totals, existing=existing)


def _abort404():
    from flask import abort
    abort(404)
