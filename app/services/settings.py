"""Key/value AppSetting helpers.

Settings are seeded on first run but always editable by Admin afterward.
Callers should always read through here rather than caching values, since
they can change at runtime (most importantly the go-live date).
"""
from datetime import date, datetime

from flask import current_app

from app.extensions import db
from app.models import AppSetting

GO_LIVE_DATE_KEY = "go_live_date"
ORG_NAME_KEY = "org_name"
CURRENT_FINANCIAL_YEAR_KEY = "current_financial_year"

DEFAULTS = {
    ORG_NAME_KEY: "SPIDQAH",
}


def get_setting(key, default=None):
    row = AppSetting.query.filter_by(key=key).first()
    if row is None or row.value is None:
        return default
    return row.value


def set_setting(key, value, user_id=None):
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    row.updated_by_id = user_id
    return row


def get_go_live_date() -> date:
    raw = get_setting(GO_LIVE_DATE_KEY)
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return current_app.config["DEFAULT_GO_LIVE_DATE"]


def set_go_live_date(new_date: date, user_id=None):
    set_setting(GO_LIVE_DATE_KEY, new_date.isoformat(), user_id=user_id)


def ensure_defaults():
    """Seed default settings if they do not already exist. Idempotent."""
    changed = False
    if AppSetting.query.filter_by(key=GO_LIVE_DATE_KEY).first() is None:
        set_setting(GO_LIVE_DATE_KEY, current_app.config["DEFAULT_GO_LIVE_DATE"].isoformat())
        changed = True
    for key, value in DEFAULTS.items():
        if AppSetting.query.filter_by(key=key).first() is None:
            set_setting(key, value)
            changed = True
    if changed:
        db.session.commit()
