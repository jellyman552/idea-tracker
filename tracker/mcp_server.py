"""MCP server so Claude can create projects and post progress from chats.

Served by tracker/asgi.py next to the web app (same process, same database).
Every change goes through `services`, so it lands in the audit log with
source "mcp". Auth is a long secret token in the URL path: /mcp/<MCP_TOKEN>.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import activity, create_app, reviews, services
from .models import PROJECT_TYPES, STATUSES, Milestone, ProgressLog, Project, db

ACTOR = services.Actor(user="claude", source="mcp")

INSTRUCTIONS = f"""\
Idea Tracker holds the user's projects (types: {", ".join(PROJECT_TYPES)};
statuses: {", ".join(STATUSES)}), each with milestones that drive a progress bar
and dated progress notes.

- Before creating a project, ask the user for the link to the current chat
  (you can't see it yourself) and pass it as chat_link.
- Each project has one designated chat thread (its chat_link). When you start
  working on a project whose chat_link is empty, ask the user whether this chat
  is its designated thread; if so, ask for the link and save it with
  update_project. Don't replace an existing chat_link unless the user asks.
- Call list_projects before creating one, to avoid duplicates.
- Archiving hides an item (read-only); unarchive_* restores it. Nothing is ever
  deleted. Still confirm with the user before archiving.
- Ticking the last open milestone moves a project to done automatically.
- When it fits the conversation, check get_stale_projects and ask the user about
  quiet projects: keep (snooze_project), add progress, or archive.
- For a weekly/monthly/yearly review, call get_review, walk the user through it,
  ask the reflection prompts, then save their answers with save_review_reflection.
- Auto-notes: when a conversation about a tracked project reaches a decision,
  finishes something, or changes plans, add a short dated note with add_note
  (see its description). Don't log small talk or things already recorded.
"""

app = None  # created on first use; tests swap in their own
mcp = MCPServer(name="Idea Tracker", instructions=INSTRUCTIONS)

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Archiving only hides things (unarchive_* restores them), so it isn't destructive.
ARCHIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _ctx():
    global app
    if app is None:
        app = create_app()
    return app.app_context()


def _today():
    return datetime.now(ZoneInfo(app.config["TIMEZONE"])).date()


# ---------- Serialisation ----------

def _progress(p):
    pr = p.progress
    return {"done": pr[0], "total": pr[1], "percent": pr[2]} if pr else None


def _project_summary(p):
    latest = p.latest_note
    return {
        "id": p.id,
        "title": p.title,
        "type": p.type,
        "status": p.status,
        "progress": _progress(p),
        "updated_at": p.updated_at.isoformat(timespec="minutes"),
        "latest_note": {"date": latest.date.isoformat(), "note": latest.note} if latest else None,
        "archived": p.archived_at is not None,
    }


def _project_detail(p, note_limit=20):
    return {
        **_project_summary(p),
        "summary": p.summary or "",
        "chat_link": p.chat_link or "",
        "repo_url": p.repo_url or "",
        "recent_commits": [{"sha": c.sha[:7], "message": c.headline, "date": c.committed_at.date().isoformat()}
                           for c in p.commits.limit(5)],
        "created_at": p.created_at.isoformat(timespec="minutes"),
        "milestones": [{"id": m.id, "title": m.title, "done": m.done} for m in p.active_milestones],
        "notes": [{"id": n.id, "date": n.date.isoformat(), "note": n.note}
                  for n in p.active_logs[:note_limit]],
        "archived_milestones": [{"id": m.id, "title": m.title} for m in p.milestones if m.archived_at],
        "archived_notes": [{"id": n.id, "date": n.date.isoformat(), "note": n.note} for n in p.archived_logs],
    }


def _get(model, obj_id, what):
    obj = db.session.get(model, obj_id)
    if obj is None:
        raise ValueError(f"No {what} with id {obj_id}.")
    return obj


def _commit(result):
    db.session.commit()
    return result


# ---------- Read tools ----------

@mcp.tool(annotations=READ)
def list_projects(status: str = "", type: str = "", query: str = "",
                  include_archived: bool = False) -> list[dict]:
    """List projects, most recently updated first. Optionally filter by status,
    type, or a case-insensitive text query on the title and summary."""
    with _ctx():
        q = Project.query
        if not include_archived:
            q = q.filter(Project.archived_at.is_(None))
        if status:
            q = q.filter(Project.status == status)
        if type:
            q = q.filter(Project.type == type)
        if query:
            like = f"%{query}%"
            q = q.filter(Project.title.ilike(like) | Project.summary.ilike(like))
        return [_project_summary(p) for p in q.order_by(Project.updated_at.desc())]


@mcp.tool(annotations=READ)
def get_project(project_id: int) -> dict:
    """Full details of one project: summary, chat link, milestones (with ids for
    ticking), the 20 most recent progress notes (with ids), and archived
    milestones and notes (with ids, for unarchiving)."""
    with _ctx():
        return _project_detail(_get(Project, project_id, "project"))


@mcp.tool(annotations=READ)
def get_stale_projects() -> list[dict]:
    """Projects that have gone quiet: no activity for 14+ days (active, paused) or
    60+ days (idea). Each has days_since_activity and its latest note, so you can
    ask the user whether to keep it (snooze_project), update it, or archive it."""
    with _ctx():
        projects = Project.query.filter(Project.archived_at.is_(None)).all()
        info = activity.staleness(projects)
        stale = [p for p in projects if info[p.id]["stale"]]
        stale.sort(key=lambda p: info[p.id]["days"], reverse=True)
        return [{**_project_summary(p), "days_since_activity": info[p.id]["days"],
                 "last_activity": info[p.id]["last_activity"].isoformat(timespec="minutes")}
                for p in stale]


@mcp.tool(annotations=READ)
def get_review(period: str = "weekly", offset: int = 0) -> dict:
    """Stats for a review period, the same numbers as the web Reviews tab.

    period: "weekly" (Mon-Sun), "monthly" or "yearly", in Pacific time.
    offset: 0 = current period, -1 = previous, and so on.
    Returns totals (and the previous period's, for comparison), projects touched,
    milestones completed, status changes, stale projects, plus the reflection
    prompts and any saved answers."""
    if offset > 0:
        raise ValueError("offset must be 0 (current period) or negative (past periods).")
    with _ctx():
        return reviews.review(period, offset)


# ---------- Write tools ----------

@mcp.tool(annotations=WRITE)
def create_project(title: str, type: str, summary: str, chat_link: str,
                   status: str = "idea", milestones: list[str] | None = None,
                   repo_url: str = "") -> dict:
    """Create a project from the current conversation.

    Ask the user for this chat's link first (e.g. https://claude.ai/chat/... or
    https://chatgpt.com/c/...) and pass
    it as chat_link; pass "" only if the user says not to link one. `type` is one
    of business, software, work, career, personal, DIY. Optionally seed the
    checklist with `milestones` (short titles, in order). `repo_url` optionally
    links a GitHub repo (https://github.com/owner/repo); its commits then sync in."""
    with _ctx():
        project = services.create_project(ACTOR, {
            "title": title, "type": type, "status": status,
            "summary": summary, "chat_link": chat_link, "repo_url": repo_url,
        })
        for t in milestones or []:
            services.add_milestone(ACTOR, project, t)
        return _commit(_project_detail(project))


@mcp.tool(annotations=WRITE)
def update_project(project_id: int, title: str | None = None, type: str | None = None,
                   status: str | None = None, summary: str | None = None,
                   chat_link: str | None = None, repo_url: str | None = None) -> dict:
    """Change a project's title, type, status (idea, active, paused, done),
    summary, chat link or GitHub repo (https://github.com/owner/repo, "" to
    unlink). Only the fields you pass are changed."""
    with _ctx():
        project = _get(Project, project_id, "project")
        fields = {k: v for k, v in {"title": title, "type": type, "status": status,
                                    "summary": summary, "chat_link": chat_link,
                                    "repo_url": repo_url}.items()
                  if v is not None}
        changes = services.update_project(ACTOR, project, fields)
        return _commit({"changed": sorted(changes), "project": _project_summary(project)})


@mcp.tool(annotations=WRITE)
def snooze_project(project_id: int) -> dict:
    """The user wants to keep a stale project as it is: resets its stale clock
    without adding a note."""
    with _ctx():
        project = _get(Project, project_id, "project")
        services.snooze_project(ACTOR, project)
        return _commit({"snoozed": project.title})


@mcp.tool(annotations=WRITE)
def add_note(project_id: int, note: str, date: str = "") -> dict:
    """Post a progress note to a project's timeline. `date` is YYYY-MM-DD and
    defaults to today.

    Use this on your own initiative (no need to ask) when, in this conversation,
    the user made a decision, finished something, or changed plans on a tracked
    project: write 1-2 plain sentences summarising it, e.g. "Decided to price the
    study pack at $15 and launch after the equilibrium worksheet." Skip small talk,
    brainstorming that went nowhere, and anything get_project shows is already
    noted. Tell the user briefly that you logged it. Notes you write are labelled
    "Claude" in the app."""
    with _ctx():
        project = _get(Project, project_id, "project")
        log = services.add_note(ACTOR, project, note, date or _today())
        return _commit({"id": log.id, "date": log.date.isoformat(), "project": _project_summary(project)})


@mcp.tool(annotations=WRITE)
def update_note(note_id: int, note: str, date: str) -> dict:
    """Correct an existing progress note's text or date (YYYY-MM-DD)."""
    with _ctx():
        log = _get(ProgressLog, note_id, "note")
        changes = services.update_note(ACTOR, log, note, date)
        return _commit({"changed": sorted(changes)})


@mcp.tool(annotations=WRITE)
def add_milestones(project_id: int, titles: list[str]) -> dict:
    """Append milestones (short titles, in order) to a project's checklist."""
    with _ctx():
        project = _get(Project, project_id, "project")
        for t in titles:
            services.add_milestone(ACTOR, project, t)
        return _commit({"milestones": _project_detail(project)["milestones"],
                        "progress": _progress(project)})


@mcp.tool(annotations=WRITE)
def set_milestone_done(milestone_id: int, done: bool = True) -> dict:
    """Tick (done=true) or untick a milestone. Ticking the last open one moves the
    project to done; unticking on a done project moves it back."""
    with _ctx():
        milestone = _get(Milestone, milestone_id, "milestone")
        new_status = services.set_milestone_done(ACTOR, milestone, done)
        return _commit({"status_changed_to": new_status,
                        "project": _project_summary(milestone.project)})


@mcp.tool(annotations=WRITE)
def save_review_reflection(period: str, offset: int, answers: dict[str, str]) -> dict:
    """Save the user's reflection for a review period after talking it through.

    answers maps each prompt (exact text from get_review's reflection.prompts) to
    the user's answer in their own words. Prompts you leave out keep their saved
    answer; an empty string clears one."""
    if offset > 0:
        raise ValueError("offset must be 0 (current period) or negative (past periods).")
    with _ctx():
        saved = reviews.save_reflection(period, offset, answers)
        return {"period": reviews.compute(period, offset)["period"], "answers": saved}


# ---------- Archive / unarchive (nothing is ever deleted) ----------

@mcp.tool(annotations=ARCHIVE)
def archive_project(project_id: int) -> dict:
    """Archive a project: hides it from the dashboard and makes it read-only.
    Restorable with unarchive_project. Confirm with the user before calling."""
    with _ctx():
        project = _get(Project, project_id, "project")
        services.archive_project(ACTOR, project)
        return _commit({"archived": project.title})


@mcp.tool(annotations=ARCHIVE)
def archive_note(note_id: int) -> dict:
    """Archive a progress note (hidden, restorable with unarchive_note). Confirm
    with the user before calling."""
    with _ctx():
        log = _get(ProgressLog, note_id, "note")
        services.archive_note(ACTOR, log)
        return _commit({"archived": log.note})


@mcp.tool(annotations=ARCHIVE)
def archive_milestone(milestone_id: int) -> dict:
    """Archive a milestone (removed from the checklist and progress; restorable
    with unarchive_milestone). Confirm with the user before calling."""
    with _ctx():
        milestone = _get(Milestone, milestone_id, "milestone")
        services.archive_milestone(ACTOR, milestone)
        return _commit({"archived": milestone.title})


@mcp.tool(annotations=WRITE)
def unarchive_project(project_id: int) -> dict:
    """Restore an archived project to the dashboard. Notes and milestones that were
    archived on their own stay archived. Find ids with list_projects(include_archived=true)."""
    with _ctx():
        project = _get(Project, project_id, "project")
        services.unarchive_project(ACTOR, project)
        return _commit(_project_summary(project))


@mcp.tool(annotations=WRITE)
def unarchive_note(note_id: int) -> dict:
    """Restore an archived progress note (ids are in get_project's archived_notes)."""
    with _ctx():
        log = _get(ProgressLog, note_id, "note")
        services.unarchive_note(ACTOR, log)
        return _commit({"restored": log.note})


@mcp.tool(annotations=WRITE)
def unarchive_milestone(milestone_id: int) -> dict:
    """Restore an archived milestone (ids are in get_project's archived_milestones)."""
    with _ctx():
        milestone = _get(Milestone, milestone_id, "milestone")
        services.unarchive_milestone(ACTOR, milestone)
        return _commit({"restored": milestone.title, "progress": _progress(milestone.project)})


# ---------- HTTP app ----------

def http_app():
    token = os.environ.get("MCP_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("Set MCP_TOKEN to a random string of at least 32 characters.")
    _ctx()  # create the Flask app once at startup, not racing on the first requests
    # Hostnames the server answers to (DNS-rebinding protection). Defaults to DOMAIN.
    raw = os.environ.get("MCP_ALLOWED_HOSTS") or os.environ.get("DOMAIN", "")
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{token}",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts + ["127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{h}" for h in hosts] + ["https://claude.ai", "https://chatgpt.com"],
        ),
    )
