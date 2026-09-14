import json
from decimal import Decimal

from flask import request
from flask_login import current_user

from app.extensions import db
from app.models import AuditLog, AuditAction


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value)
    return str(value)


def _dump(data):
    if data is None:
        return None
    return json.dumps(data, default=_json_default, sort_keys=True)


def log_audit(entity_type, entity_id, action: AuditAction, before=None, after=None, reason=None, user=None):
    user = user if user is not None else (current_user if getattr(current_user, "is_authenticated", False) else None)
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_json=_dump(before),
        after_json=_dump(after),
        reason=reason,
        user_id=getattr(user, "id", None),
        ip_address=request.remote_addr if request else None,
    )
    db.session.add(entry)
    return entry
