"""Safe, idempotent bulk import of Contributor NAME-ONLY records.

Deliberately narrow: creates permanent ``Contributor`` rows only (name,
active=True, no phone, no notes) so they become selectable via the
collection-entry autocomplete. Never creates a ``ContributionTransaction``,
never carries a pledge/target amount, never imports a phone number.

Duplicate protection is exact-match only (Contributor.normalize(): case
-insensitive, whitespace-collapsed) - the same rule the rest of the app
already uses for the "already exists" warning during quick entry. This
importer never fuzzy-merges: two similarly-spelled names are reported
as a warning (see find_similar()) but both are still created/kept as
distinct contributors unless a human decides otherwise (e.g. via the
existing Admin -> Merge Duplicate Contributors screen).
"""
import difflib
import json

from app.extensions import db
from app.models import Contributor, AuditAction
from app.services.contributors import find_exact, find_similar
from app.services.audit import log_audit

ACTION_CREATE = "CREATE"
ACTION_SKIP_EXISTING = "SKIP_EXISTING"
ACTION_SKIP_DUPLICATE_IN_PAYLOAD = "SKIP_DUPLICATE_IN_PAYLOAD"


class PayloadError(ValueError):
    """Raised for structurally invalid input. Never partially applied."""


def parse_names(raw_text):
    """Accepts either newline-separated plain text, or JSON (a list of
    strings, a list of {"name": ...} objects, or {"names": [...]}).
    Returns the raw name strings in order, exactly as given - not yet
    deduped or validated."""
    raw_text = raw_text or ""
    stripped = raw_text.strip()
    if not stripped:
        raise PayloadError("No names supplied.")

    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"Input looks like JSON but failed to parse: {exc}")
        if isinstance(data, dict):
            data = data.get("names")
        if not isinstance(data, list):
            raise PayloadError('JSON payload must be a list of names, or {"names": [...]}.')
        names = []
        for entry in data:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
            else:
                raise PayloadError(f"Invalid name entry in JSON payload: {entry!r}")
        return names

    return stripped.splitlines()


def build_plan(raw_names):
    """Read-only: validates, dedupes, and decides what WOULD happen for
    each name. Makes no writes.

    Similar-name warnings are checked against BOTH existing active DB
    contributors AND other names earlier in this same payload - two
    new, similarly-spelled names (e.g. "TEST PERSON ALPHA" / "TEST PERSON ALFA")
    would otherwise slip past a DB-only check since neither exists yet.
    Warnings never block or merge anything; both are still created.
    """
    plan = []
    seen_in_payload = set()
    payload_names_so_far = {}  # normalized -> original name, for in-payload similarity checks

    for raw in raw_names:
        name = (raw or "").strip()
        if not name:
            continue
        normalized = Contributor.normalize(name)
        if not normalized:
            continue

        if normalized in seen_in_payload:
            plan.append({
                "name": name, "normalized": normalized,
                "action": ACTION_SKIP_DUPLICATE_IN_PAYLOAD, "similar": [],
            })
            continue
        seen_in_payload.add(normalized)

        existing = find_exact(name)
        if existing:
            plan.append({
                "name": name, "normalized": normalized,
                "action": ACTION_SKIP_EXISTING, "existing_id": existing.id, "similar": [],
            })
            payload_names_so_far[normalized] = name
            continue

        similar_in_db = [{"id": c.id, "name": c.name} for c in find_similar(name)]
        similar_in_payload = [
            {"id": None, "name": other_name}
            for other_normalized, other_name in payload_names_so_far.items()
            if other_normalized != normalized
            and difflib.get_close_matches(normalized, [other_normalized], n=1, cutoff=0.72)
        ]
        plan.append({
            "name": name, "normalized": normalized, "action": ACTION_CREATE,
            "similar": similar_in_db + similar_in_payload,
        })
        payload_names_so_far[normalized] = name

    return plan


def apply_plan(plan, user, source_note=None):
    """Writes every CREATE action in one transaction - all or nothing.
    Never creates a ContributionTransaction or any financial record."""
    results = []
    try:
        for item in plan:
            if item["action"] != ACTION_CREATE:
                results.append({**item, "applied": "skipped"})
                continue

            contributor = Contributor(
                name=item["name"], name_normalized=item["normalized"],
                active=True, created_by_id=getattr(user, "id", None),
            )
            db.session.add(contributor)
            db.session.flush()
            log_audit(
                "Contributor", contributor.id, AuditAction.CREATE,
                after={"name": contributor.name}, reason=source_note, user=user,
            )
            results.append({**item, "applied": "created", "id": contributor.id})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return results
