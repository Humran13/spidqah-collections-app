from datetime import date, datetime

from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db, csrf
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


def _day_totals(day):
    rows = (
        db.session.query(ContributionTransaction.collection_type, func.coalesce(func.sum(ContributionTransaction.amount), 0))
        .filter(ContributionTransaction.date == day, ContributionTransaction.status == TransactionStatus.ACTIVE)
        .group_by(ContributionTransaction.collection_type)
        .all()
    )
    totals = {"MUKULULO": 0, "FRIDAY": 0, "SUNDAY": 0}
    for ctype, total in rows:
        totals[ctype.value] = int(total)
    totals["OVERALL"] = totals["MUKULULO"] + totals["FRIDAY"] + totals["SUNDAY"]
    return totals


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
        totals=_day_totals(selected_date),
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
        "totals": _day_totals(entry_date),
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


@collections_bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(*EDIT_ROLES)
def edit_transaction(txn_id):
    txn = db.session.get(ContributionTransaction, txn_id) or _abort404()
    if txn.status == TransactionStatus.VOID:
        flash("Voided transactions cannot be edited.", "warning")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    form = EditTransactionForm(obj=txn)
    if request.method == "GET":
        form.amount.data = str(txn.amount)

    if form.validate_on_submit():
        try:
            new_amount = parse_amount_ugx(form.amount.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("collections/edit_transaction.html", form=form, txn=txn)

        before = {
            "date": txn.date.isoformat(), "collection_type": txn.collection_type.value,
            "amount": txn.amount, "note": txn.note,
        }
        txn.date = form.date.data
        txn.collection_type = CollectionType[form.collection_type.data]
        txn.amount = new_amount
        txn.note = (form.note.data or "").strip() or None
        txn.updated_by_id = current_user.id
        txn.updated_at = datetime.utcnow()
        after = {
            "date": txn.date.isoformat(), "collection_type": txn.collection_type.value,
            "amount": txn.amount, "note": txn.note,
        }
        log_audit("ContributionTransaction", txn.id, AuditAction.EDIT, before=before, after=after, reason=form.reason.data)
        db.session.commit()
        flash("Transaction updated.", "success")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    return render_template("collections/edit_transaction.html", form=form, txn=txn)


@collections_bp.route("/transactions/<int:txn_id>/void", methods=["GET", "POST"])
@login_required
@roles_required(*EDIT_ROLES)
def void_transaction(txn_id):
    txn = db.session.get(ContributionTransaction, txn_id) or _abort404()
    form = VoidTransactionForm()
    if txn.status == TransactionStatus.VOID:
        flash("Transaction is already void.", "info")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

    if form.validate_on_submit():
        before = {"status": txn.status.value}
        txn.status = TransactionStatus.VOID
        txn.voided_by_id = current_user.id
        txn.voided_at = datetime.utcnow()
        txn.void_reason = form.reason.data
        log_audit("ContributionTransaction", txn.id, AuditAction.VOID, before=before,
                   after={"status": "VOID"}, reason=form.reason.data)
        db.session.commit()
        flash("Transaction voided.", "success")
        return redirect(url_for("collections.history", date=txn.date.isoformat()))

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
    from app.models import CollectionSession, SessionStatus

    day = request.args.get("date")
    try:
        selected_date = datetime.strptime(day, "%Y-%m-%d").date() if day else date.today()
    except ValueError:
        selected_date = date.today()

    existing = CollectionSession.query.filter_by(date=selected_date).first()
    form = SessionCloseForm(date=selected_date)
    if existing and request.method == "GET":
        form.physical_cash_counted.data = str(existing.physical_cash_counted or "")
        form.notes.data = existing.notes

    totals = _day_totals(selected_date)

    if form.validate_on_submit():
        try:
            physical = parse_amount_ugx(form.physical_cash_counted.data)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("collections/session.html", form=form, selected_date=selected_date,
                                    totals=totals, existing=existing)

        session_row = existing or CollectionSession(date=form.date.data, created_by_id=current_user.id)
        session_row.date = form.date.data
        session_row.mukululo_system_total = totals["MUKULULO"]
        session_row.friday_system_total = totals["FRIDAY"]
        session_row.sunday_system_total = totals["SUNDAY"]
        session_row.physical_cash_counted = physical
        session_row.difference = physical - totals["OVERALL"]
        if session_row.difference == 0:
            session_row.status = SessionStatus.BALANCED
        elif session_row.difference > 0:
            session_row.status = SessionStatus.OVER
        else:
            session_row.status = SessionStatus.SHORT
        session_row.notes = (form.notes.data or "").strip() or None
        session_row.updated_at = datetime.utcnow()
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
