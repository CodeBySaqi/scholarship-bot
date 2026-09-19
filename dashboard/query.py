"""Read/write queries behind the Scholar Radar pages.

Nothing in here builds HTML — the same functions answer the UI, `radar.py
query`, and the tests, so a filter that works in the page works in CI.
"""

from __future__ import annotations

import contextlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select

from db.models import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_OUTDATED,
    STATUS_STALE,
    NotificationLog,
    Scholarship,
    SourceRun,
    utcnow,
)

LIST_FIELDS = (
    "id", "title", "url", "source", "provider", "university", "country", "country_scope",
    "degree_level", "field", "funding_type", "amount_text", "amount_usd", "currency",
    "application_fee_usd", "intake", "round_label", "deadline", "deadline_text",
    "deadline_rolling", "deadline_estimated", "match_score", "tier", "confidence",
    "gate_pass", "status", "starred", "hidden", "notes", "is_new", "changed",
    "notified", "times_seen", "first_seen_at", "last_seen_at",
)

SORTS = {
    "score": Scholarship.match_score.desc(),
    "-score": Scholarship.match_score.asc(),
    "deadline": Scholarship.deadline.asc(),
    "-deadline": Scholarship.deadline.desc(),
    "amount": Scholarship.amount_usd.desc(),
    "-amount": Scholarship.amount_usd.asc(),
    "seen": Scholarship.last_seen_at.desc(),
    "title": Scholarship.title.asc(),
    "days": Scholarship.deadline.asc(),
}

STATUS_FILTERS = {
    "active": [STATUS_ACTIVE],
    "expiring": [STATUS_ACTIVE],
    "expired": [STATUS_EXPIRED],
    "closed": [STATUS_OUTDATED, STATUS_STALE, STATUS_EXPIRED],
    "any": None,
}


def _in_window(days_left: int | None, days_max: int | None) -> bool:
    """`0 <= days_left <= days_max`, with None meaning “don’t filter”."""
    return days_max is None or (days_left is not None and 0 <= days_left <= days_max)


def _decode(row: Scholarship, key: str) -> Any:
    raw = getattr(row, key, None)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:  # pragma: no cover - defensive
        return []


def list_scholarships(
    session,
    *,
    search: str = "",
    country: str = "",
    degree: str = "",
    tier: str = "",
    source: str = "",
    funding: str = "",
    status: str = "active",
    gate: str = "",
    days_max: int | None = None,
    min_amount: float | None = None,
    starred_only: bool = False,
    include_hidden: bool = False,
    sort: str = "score",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Filter, sort and page the archive. Returns {rows, total, facets}."""
    stmt = select(Scholarship)
    if not include_hidden:
        stmt = stmt.where(Scholarship.hidden.isnot(True))
    if starred_only:
        stmt = stmt.where(Scholarship.starred.is_(True))

    statuses = STATUS_FILTERS.get(status or "any")
    if statuses:
        stmt = stmt.where(Scholarship.status.in_(statuses))
    if status == "expiring":
        # Pushed into SQL so `total` describes the same set the page lists — a
        # count of everything and a filtered list would read like a bug.
        horizon = int(days_max) if days_max is not None else 45
        now = utcnow()
        stmt = stmt.where(Scholarship.deadline.isnot(None),
                           Scholarship.deadline >= now,
                           Scholarship.deadline <= now + timedelta(days=horizon))
        days_max = None

    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Scholarship.title.ilike(like),
            Scholarship.provider.ilike(like),
            Scholarship.university.ilike(like),
            Scholarship.description.ilike(like),
            Scholarship.coverage.ilike(like),
            Scholarship.country.ilike(like),
            Scholarship.field.ilike(like),
        ))
    for col, val in ((Scholarship.country, country), (Scholarship.degree_level, degree),
                     (Scholarship.tier, tier), (Scholarship.funding_type, funding)):
        if val and val != "any":
            stmt = stmt.where(col == val)
    if source and source != "any":
        stmt = stmt.where(or_(Scholarship.source == source, Scholarship.seen_in_sources.ilike(f"%{source}%")))
    if gate == "pass":
        stmt = stmt.where(Scholarship.gate_pass.is_(True))
    elif gate == "fail":
        stmt = stmt.where(Scholarship.gate_pass.is_(False))

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    order = [SORTS.get(sort, SORTS["score"])]
    if sort in {"score", "days", "deadline"}:
        order.append(Scholarship.deadline.asc().nulls_last())
    rows = session.execute(
        stmt.order_by(*order).limit(max(1, min(int(limit), 500))).offset(max(0, int(offset)))
    ).scalars().all()

    out = []
    for row in rows:
        item = {f: getattr(row, f) for f in LIST_FIELDS}
        item["deadline"] = row.deadline.strftime("%Y-%m-%d") if row.deadline else None
        item["first_seen_at"] = row.first_seen_at.strftime("%Y-%m-%d") if row.first_seen_at else None
        item["last_seen_at"] = row.last_seen_at.strftime("%Y-%m-%d %H:%M") if row.last_seen_at else None
        item["days_left"] = row.days_left
        item["coverage"] = _decode(row, "coverage")
        item["gate_reasons"] = _decode(row, "gate_reasons")
        item["eligible_countries"] = _decode(row, "eligible_countries")
        item["quality_flags"] = _decode(row, "quality_flags")
        out.append(item)

    return {"rows": out, "total": int(total), "limit": limit, "offset": offset, "facets": facets(session)}


def facets(session) -> dict[str, list[dict[str, Any]]]:
    """Dropdown contents: every country/degree/source/tier in the DB with counts."""

    def grouped(col):
        pairs = session.execute(
            select(col, func.count()).where(col.isnot(None), col != "").group_by(col).order_by(func.count().desc())
        ).all()
        return [{"value": p[0], "count": int(p[1])} for p in pairs[:60]]

    return {
        "country": grouped(Scholarship.country),
        "degree_level": grouped(Scholarship.degree_level),
        "tier": grouped(Scholarship.tier),
        "source": grouped(Scholarship.source),
        "funding_type": grouped(Scholarship.funding_type),
    }


def get_scholarship(session, key: str | int) -> dict[str, Any] | None:
    """One row in full, addressed by id or by any substring of its URL."""
    row = None
    with contextlib.suppress(TypeError, ValueError):
        row = session.get(Scholarship, int(key))
    if row is None:
        like = f"%{key}%"
        row = session.execute(
            select(Scholarship).where(or_(Scholarship.url.ilike(like), Scholarship.title.ilike(like)))
            .order_by(Scholarship.match_score.desc()).limit(1)
        ).scalars().first()
    if row is None:
        return None
    data = row.as_dict()
    data["id"] = row.id
    data["starred"] = bool(row.starred)
    data["hidden"] = bool(row.hidden)
    data["notes"] = row.notes or ""
    data["gate_reasons"] = _decode(row, "gate_reasons")
    data["quality_flags"] = _decode(row, "quality_flags")
    try:
        data["score_breakdown"] = json.loads(row.score_breakdown or "{}")
    except Exception:  # pragma: no cover
        data["score_breakdown"] = {}
    try:
        data["extras"] = json.loads(row.extras or "{}") if isinstance(row.extras, str) else (row.extras or {})
    except Exception:  # pragma: no cover
        data["extras"] = {}
    if row.deadline:
        data["days_left"] = row.days_left
    return data


def set_flags(session, key: int | str, *, starred: bool | None = None, hidden: bool | None = None,
              notes: str | None = None) -> dict[str, Any]:
    row = session.get(Scholarship, int(key)) if str(key).isdigit() else None
    if row is None:
        raise KeyError(f"no scholarship with id {key!r}")
    if starred is not None:
        row.starred = bool(starred)
    if hidden is not None:
        row.hidden = bool(hidden)
        if hidden:
            row.starred = False
    if notes is not None:
        row.notes = str(notes)[:2000] or None
    session.commit()
    return {"id": row.id, "starred": bool(row.starred), "hidden": bool(row.hidden), "notes": row.notes or ""}


def archive_stats(session) -> dict[str, Any]:
    """Headline numbers for the overview page."""
    total = session.execute(select(func.count()).select_from(Scholarship)).scalar_one()
    active = session.execute(select(func.count()).select_from(Scholarship).where(
        Scholarship.status == STATUS_ACTIVE, Scholarship.hidden.isnot(True))).scalar_one()
    open_gates = session.execute(select(func.count()).select_from(Scholarship).where(
        Scholarship.status == STATUS_ACTIVE, Scholarship.gate_pass.is_(True),
        Scholarship.hidden.isnot(True))).scalar_one()
    starred = session.execute(select(func.count()).select_from(Scholarship).where(
        Scholarship.starred.is_(True))).scalar_one()
    hidden = session.execute(select(func.count()).select_from(Scholarship).where(
        Scholarship.hidden.is_(True))).scalar_one()
    funded = session.execute(select(func.max(Scholarship.amount_usd))).scalar_one()
    soonest = session.execute(select(func.min(Scholarship.deadline)).where(
        Scholarship.status == STATUS_ACTIVE, Scholarship.deadline.isnot(None))).scalar_one()
    last_run = session.execute(select(SourceRun).order_by(SourceRun.id.desc()).limit(1)).scalars().first()
    last_notify = session.execute(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(1)).scalars().first()
    by_tier = dict(session.execute(
        select(Scholarship.tier, func.count()).where(Scholarship.status == STATUS_ACTIVE)
        .group_by(Scholarship.tier)).all())
    return {
        "rows": int(total or 0),
        "active": int(active or 0),
        "gate_pass": int(open_gates or 0),
        "starred": int(starred or 0),
        "hidden": int(hidden or 0),
        "max_award_usd": float(funded) if funded else None,
        "soonest_deadline": soonest.strftime("%Y-%m-%d") if soonest else None,
        "by_tier": {str(k or "?"): int(v) for k, v in by_tier.items()},
        "last_source_run": None if last_run is None else {
            "started_at": last_run.started_at.strftime("%Y-%m-%d %H:%M") if last_run.started_at else None,
            "status": last_run.status, "source": last_run.source,
        },
        "last_notification": None if last_notify is None else {
            "created_at": last_notify.created_at.strftime("%Y-%m-%d %H:%M") if last_notify.created_at else None,
            "channel": last_notify.channel, "status": last_notify.status,
            "recipient": last_notify.recipient, "count": last_notify.count,
            "detail": (last_notify.detail or "")[:300],
        },
    }
