"""Activity history: the audit log read as named events, plus GitHub commits.

There is no separate activity table. Every change already goes through
`services` and lands in `AuditLog` (UTC timestamp, source, old -> new), so this
module names those rows and merges in synced commits (source "github").
Stale detection and reviews are built on it.
"""

from dataclasses import dataclass
from datetime import datetime

from flask import current_app
from sqlalchemy import func

from .models import AuditLog, Commit, db, utcnow

PAST = {"archive": "archived", "unarchive": "unarchived", "snooze": "snoozed"}


@dataclass
class Event:
    ts: datetime  # naive UTC
    project_id: int
    type: str  # created, status_changed, milestone_done, note_added, archived, commit, ...
    source: str  # web, cli, mcp, github
    from_value: str | None = None
    to_value: str | None = None
    entity_id: int | None = None  # the note / milestone / project the event is about


def _change(row, field):
    old, new = (row.changes or {}).get(field, (None, None))
    return old, new


def event_from_audit(row):
    """Name one audit row. Status changes win over other edits in the same row."""
    entity, action = row.entity, row.action

    def ev(type_, from_value=None, to_value=None):
        return Event(row.ts, row.project_id, type_, row.source, from_value, to_value, row.entity_id)

    if entity == "project":
        if action == "create":
            return ev("created", None, _change(row, "status")[1])
        if action == "update":
            if "status" in (row.changes or {}):
                return ev("status_changed", *_change(row, "status"))
            return ev("project_edited", None, ", ".join(sorted(row.changes or {})))
        return ev(PAST[action])  # archived, unarchived, snoozed

    title_field = "note" if entity == "note" else "title"
    label = _change(row, title_field)[1]
    if action == "create":
        return ev(f"{entity}_added", None, label)
    if action == "update" and entity == "milestone" and "done" in (row.changes or {}):
        done = _change(row, "done")[1]
        return ev("milestone_done" if done else "milestone_reopened", None, label)
    if action == "update":
        return ev(f"{entity}_edited", None, label)
    return ev(f"{entity}_{PAST[action]}", None, label)  # note_archived, milestone_unarchived, ...


def _window(q, ts_col, project_col, start, end, project_id):
    if start is not None:
        q = q.filter(ts_col >= start)
    if end is not None:
        q = q.filter(ts_col < end)
    if project_id is not None:
        q = q.filter(project_col == project_id)
    return q


def events(start=None, end=None, project_id=None):
    """Events in [start, end) (naive UTC datetimes), oldest first."""
    audit = _window(AuditLog.query, AuditLog.ts, AuditLog.project_id, start, end, project_id)
    commits = _window(Commit.query, Commit.committed_at, Commit.project_id, start, end, project_id)
    evs = [event_from_audit(r) for r in audit.order_by(AuditLog.ts, AuditLog.id)]
    evs += [Event(c.committed_at, c.project_id, "commit", "github", None, c.headline, c.id) for c in commits]
    return sorted(evs, key=lambda e: e.ts)


def last_activity(project_ids=None):
    """{project_id: naive UTC datetime of its most recent event, commits included}."""
    ids = None if project_ids is None else list(project_ids)
    latest = {}
    for model, ts in ((AuditLog, AuditLog.ts), (Commit, Commit.committed_at)):
        q = db.session.query(model.project_id, func.max(ts)).group_by(model.project_id)
        if ids is not None:
            q = q.filter(model.project_id.in_(ids))
        for pid, when in q:
            latest[pid] = max(when, latest.get(pid, when))
    return latest


# ---------- Stale detection ----------

def staleness(projects, now=None):
    """{project_id: {"last_activity", "days", "stale"}} for the given projects.

    Thresholds come from STALE_AFTER_DAYS ({status: days}). Statuses missing from
    it (done) and archived projects never go stale. Snoozing counts as activity."""
    now = now or utcnow()
    thresholds = current_app.config["STALE_AFTER_DAYS"]
    last = last_activity(p.id for p in projects)
    info = {}
    for p in projects:
        ts = last.get(p.id) or p.created_at
        days = (now - ts).days
        limit = thresholds.get(p.status)
        info[p.id] = {
            "last_activity": ts,
            "days": days,
            "stale": p.archived_at is None and limit is not None and days >= limit,
        }
    return info
