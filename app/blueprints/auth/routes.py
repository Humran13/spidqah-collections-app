from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db, limiter
from app.models import User, AuditAction
from app.services.audit import log_audit
from app.blueprints.auth.forms import LoginForm, ChangePasswordForm

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()

        if user and user.locked_until and user.locked_until > datetime.utcnow():
            flash("This account is temporarily locked due to failed login attempts. Try again later.", "danger")
            return render_template("auth/login.html", form=form)

        if user and user.active and user.check_password(form.password.data):
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = datetime.utcnow()
            log_audit("User", user.id, AuditAction.LOGIN, user=user)
            db.session.commit()
            login_user(user, remember=form.remember.data)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("main.dashboard"))

        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
                user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            log_audit("User", user.id, AuditAction.LOGIN_FAILED, user=user)
            db.session.commit()

        flash("Invalid username or password.", "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password is incorrect.", "danger")
        elif form.new_password.data != form.confirm_password.data:
            flash("New passwords do not match.", "danger")
        else:
            current_user.set_password(form.new_password.data)
            log_audit("User", current_user.id, AuditAction.RESET_PASSWORD, reason="Self-service password change")
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html", form=form)
