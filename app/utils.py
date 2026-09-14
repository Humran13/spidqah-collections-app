from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import abort, current_app
from flask_login import current_user

KAMPALA_TZ = ZoneInfo("Africa/Kampala")


def format_ugx(amount) -> str:
    if amount is None:
        amount = 0
    amount = int(amount)
    sign = "-" if amount < 0 else ""
    return f"{sign}UGX {abs(amount):,}"


def to_local(dt: datetime):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(KAMPALA_TZ)


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def parse_amount_ugx(raw: str) -> int:
    """Parse a UGX amount string into a positive integer, rejecting
    decimals/floats so money is never stored as anything but whole
    shillings."""
    if raw is None:
        raise ValueError("Amount is required")
    cleaned = str(raw).replace(",", "").replace("UGX", "").strip()
    if cleaned == "":
        raise ValueError("Amount is required")
    if "." in cleaned:
        whole, _, frac = cleaned.partition(".")
        if frac.strip("0") != "":
            raise ValueError("Amounts must be whole UGX shillings, no cents")
        cleaned = whole
    if not cleaned.lstrip("-").isdigit():
        raise ValueError("Invalid amount")
    value = int(cleaned)
    if value <= 0:
        raise ValueError("Amount must be greater than zero")
    return value


def parse_amount_ugx_allow_zero(raw: str) -> int:
    """Like parse_amount_ugx but allows zero (e.g. opening balances)."""
    if raw is None or str(raw).strip() == "":
        return 0
    cleaned = str(raw).replace(",", "").replace("UGX", "").strip()
    if "." in cleaned:
        whole, _, frac = cleaned.partition(".")
        if frac.strip("0") != "":
            raise ValueError("Amounts must be whole UGX shillings, no cents")
        cleaned = whole
    if not cleaned.lstrip("-").isdigit():
        raise ValueError("Invalid amount")
    value = int(cleaned)
    if value < 0:
        raise ValueError("Amount cannot be negative")
    return value
