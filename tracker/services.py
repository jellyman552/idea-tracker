"""Every change to projects, notes and milestones goes through here so it lands
in the audit log the same way whether it came from the web UI, the CLI or MCP.
Nothing is ever deleted: archiving hides an item and unarchiving restores it."""

import re
from dataclasses import dataclass
from datetime import date

from .models import (
    PROJECT_TYPES, STATUSES, AuditLog, Milestone, ProgressLog, Project, db, utcnow,
)

PROJECT_FIELDS = ["title", "type", "status", "summary", "chat_link", "repo_url"]
CHAT_LINK_PREFIX = "https://"  # any AI chat (Claude, ChatGPT, Gemini, ...); https only
REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


class ValidationError(ValueError):
    pass


class ArchivedError(ValueError):
    pass


@dataclass
class Actor:
    user: str
    source: str = "web"


def _audit(actor, action, entity, entity_id, project_id, changes=None):
    db.session.add(AuditLog(
        user=actor.user, source=actor.source, action=action, entity=entity,
        entity_id=entity_id, project_id=project_id, changes=changes or {},
    ))


def _touch(project):
    project.updated_at = utcnow()


def _require_live(obj):
    if obj.archived_at:
        raise ArchivedError("Archived items are read-only.")


def _require_archived(obj, what):
    if not obj.archived_at:
        raise ValidationError(f"That {what} isn't archived.")


def _json(value):
    return value.isoformat() if isinstance(value, date) else value


def clean_project_fields(data):
    """Strip and validate project form/MCP input. Returns a dict of the known fields."""
    fields = {k: (data.get(k) or "").strip() for k in PROJECT_FIELDS if k in data}
    errors = []
    if "title" in fields and not fields["title"]:
        errors.append("Title is required.")
    if len(fields.get("title", "")) > 200:
        errors.append("Title must be 200 characters or fewer.")
    if "type" in fields and fields["type"] not in PROJECT_TYPES:
        errors.append(f"Type must be one of: {', '.join(PROJECT_TYPES)}.")
    if "status" in fields and fields["status"] not in STATUSES:
        errors.append(f"Status must be one of: {', '.join(STATUSES)}.")
    link = fields.get("chat_link", "")
    if link and (not link.startswith(CHAT_LINK_PREFIX) or len(link) > 500):
        errors.append("Chat link must be an https:// URL.")
    if fields.get("repo_url"):
        match = REPO_URL.match(fields["repo_url"])
        if match:
            fields["repo_url"] = "https://github.com/{}/{}".format(*match.groups())
        else:
            errors.append("Repo must look like https://github.com/owner/repo.")
    if errors:
        raise ValidationError(" ".join(errors))
    return fields


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError("Date must be YYYY-MM-DD.")


def _clean_text(value, what, limit=None):
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{what} can't be empty.")
    if limit and len(value) > limit:
        raise ValidationError(f"{what} must be {limit} characters or fewer.")
    return value


# ---------- Projects ----------

def create_project(actor, data):
    fields = clean_project_fields({"type": "personal", "status": "idea", **data})
    if "title" not in fields:
        raise ValidationError("Title is required.")
    project = Project(**fields)
    db.session.add(project)
    db.session.flush()
    _audit(actor, "create", "project", project.id, project.id,
           {k: [None, v] for k, v in fields.items() if v})
    return project


def update_project(actor, project, data):
    """Apply changed fields. Returns the {field: [old, new]} diff (empty if nothing changed)."""
    _require_live(project)
    fields = clean_project_fields(data)
    changes = {k: [getattr(project, k) or "", v] for k, v in fields.items()
               if v != (getattr(project, k) or "")}
    for k, (_, v) in changes.items():
        setattr(project, k, v)
    if changes:
        _touch(project)
        _audit(actor, "update", "project", project.id, project.id, changes)
    return changes


def archive_project(actor, project):
    _require_live(project)
    project.archived_at = utcnow()
    _audit(actor, "archive", "project", project.id, project.id, {"title": [project.title, project.title]})


def unarchive_project(actor, project):
    """Restore a project. Its notes and milestones come back with it, except ones
    archived individually, which stay archived until restored themselves."""
    _require_archived(project, "project")
    project.archived_at = None
    _touch(project)
    _audit(actor, "unarchive", "project", project.id, project.id, {"title": [project.title, project.title]})


def snooze_project(actor, project):
    """'I know, keep it': resets the stale clock without a fake note. Doesn't
    change updated_at, since nothing about the project itself changed."""
    _require_live(project)
    _audit(actor, "snooze", "project", project.id, project.id)


def _set_status(actor, project, status):
    if project.status != status:
        _audit(actor, "update", "project", project.id, project.id, {"status": [project.status, status]})
        project.status = status


def _status_before_done(project):
    """The status a project had before it was last moved to done (default: active)."""
    rows = (AuditLog.query.filter_by(project_id=project.id, entity="project", action="update")
            .order_by(AuditLog.id.desc()).all())
    for row in rows:
        old_new = row.changes.get("status")
        if old_new and old_new[1] == "done" and old_new[0] != "done":
            return old_new[0]
    return "active"


# ---------- Notes ----------

def add_note(actor, project, note, on=None):
    _require_live(project)
    log = ProgressLog(project=project, note=_clean_text(note, "Note"),
                      date=parse_date(on) if isinstance(on, str) else (on or date.today()))
    db.session.add(log)
    db.session.flush()
    _touch(project)
    _audit(actor, "create", "note", log.id, project.id,
           {"date": [None, log.date.isoformat()], "note": [None, log.note]})
    return log


def update_note(actor, log, note, on):
    _require_live(log.project)
    _require_live(log)
    new = {"note": _clean_text(note, "Note"), "date": parse_date(on) if isinstance(on, str) else on}
    changes = {k: [_json(getattr(log, k)), _json(v)] for k, v in new.items() if v != getattr(log, k)}
    for k, v in new.items():
        setattr(log, k, v)
    if changes:
        _touch(log.project)
        _audit(actor, "update", "note", log.id, log.project_id, changes)
    return changes


def archive_note(actor, log):
    _require_live(log.project)
    _require_live(log)
    log.archived_at = utcnow()
    _touch(log.project)
    _audit(actor, "archive", "note", log.id, log.project_id, {"note": [log.note, log.note]})


def unarchive_note(actor, log):
    _require_live(log.project)
    _require_archived(log, "note")
    log.archived_at = None
    _touch(log.project)
    _audit(actor, "unarchive", "note", log.id, log.project_id, {"note": [log.note, log.note]})


# ---------- Milestones ----------

def add_milestone(actor, project, title):
    _require_live(project)
    position = max((m.position for m in project.milestones), default=-1) + 1
    milestone = Milestone(project=project, title=_clean_text(title, "Milestone", 200), position=position)
    db.session.add(milestone)
    db.session.flush()
    _touch(project)
    _audit(actor, "create", "milestone", milestone.id, project.id, {"title": [None, milestone.title]})
    return milestone


def set_milestone_done(actor, milestone, done):
    """Tick or untick. Ticking the last open milestone moves the project to done;
    unticking one on a done project moves it back to its previous status.
    Returns the project's new status if it changed, else None."""
    project = milestone.project
    _require_live(project)
    _require_live(milestone)
    if milestone.done == done:
        return None
    milestone.done = done
    _touch(project)
    _audit(actor, "update", "milestone", milestone.id, project.id,
           {"done": [not done, done], "title": [milestone.title, milestone.title]})
    live = project.active_milestones
    if done and project.status != "done" and all(m.done for m in live):
        _set_status(actor, project, "done")
        return "done"
    if not done and project.status == "done":
        previous = _status_before_done(project)
        _set_status(actor, project, previous)
        return previous
    return None


def archive_milestone(actor, milestone):
    _require_live(milestone.project)
    _require_live(milestone)
    milestone.archived_at = utcnow()
    _touch(milestone.project)
    _audit(actor, "archive", "milestone", milestone.id, milestone.project_id,
           {"title": [milestone.title, milestone.title]})


def unarchive_milestone(actor, milestone):
    _require_live(milestone.project)
    _require_archived(milestone, "milestone")
    milestone.archived_at = None
    _touch(milestone.project)
    _audit(actor, "unarchive", "milestone", milestone.id, milestone.project_id,
           {"title": [milestone.title, milestone.title]})
