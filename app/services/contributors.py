import difflib

from app.models import Contributor


def find_exact(name: str):
    normalized = Contributor.normalize(name)
    return Contributor.query.filter_by(name_normalized=normalized).first()


def find_similar(name: str, limit: int = 5, cutoff: float = 0.72):
    """Cheap duplicate-warning helper: returns existing contributors whose
    normalized name is close to the given name, for a
    "Abbas already exists. Use existing contributor?" style warning.

    Does NOT auto-merge anything - callers must get explicit confirmation.
    """
    normalized = Contributor.normalize(name)
    if not normalized:
        return []
    candidates = Contributor.query.filter_by(active=True).all()
    names = {c.name_normalized: c for c in candidates}
    close = difflib.get_close_matches(normalized, names.keys(), n=limit, cutoff=cutoff)
    return [names[n] for n in close]


def search_contributors(term: str, limit: int = 10, active_only: bool = True):
    term = (term or "").strip()
    query = Contributor.query
    if active_only:
        query = query.filter_by(active=True)
    if term:
        like = f"%{Contributor.normalize(term)}%"
        query = query.filter(Contributor.name_normalized.ilike(like))
    return query.order_by(Contributor.name.asc()).limit(limit).all()
