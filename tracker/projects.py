from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from . import activity, services
from .models import (
    PROJECT_TYPES, STATUSES, AuditLog, Milestone, ProgressLog, Project, db,
)
from .services import Actor, ArchivedError, ValidationError

bp = Blueprint("projects", __name__)

SORTS = {
    "updated_at": "Last updated",
    "created_at": "Created",
    "title": "Title",
    "type": "Type",
    "status": "Status",
    "progress": "Progress",
}
# Extra table-only sort key.
TABLE_SORTS = {**SORTS, "latest": "Latest note"}
DEFAULT_DIR = {"updated_at": "desc", "created_at": "desc", "latest": "desc", "progress": "desc"}


@bp.before_request
@login_required
def require_login():
    pass


def actor():
    return Actor(user=current_user.username, source="web")


def get_project(project_id):
    return db.session.get(Project, project_id) or abort(404)


def get_live(model, obj_id):
    obj = db.session.get(model, obj_id) or abort(404)
    project = obj if isinstance(obj, Project) else obj.project
    if obj.archived_at or project.archived_at:
        abort(409, "Archived items are read-only.")
    return obj


def _sort_key(key):
    if key == "status":
        return lambda p: STATUSES.index(p.status) if p.status in STATUSES else len(STATUSES)
    if key == "type":
        return lambda p: PROJECT_TYPES.index(p.type) if p.type in PROJECT_TYPES else len(PROJECT_TYPES)
    if key == "title":
        return lambda p: p.title.lower()
    if key == "progress":
        return lambda p: p.progress[2] if p.progress else -1
    if key == "latest":
        return lambda p: p.latest_note.date.isoformat() if p.latest_note else ""
    return lambda p: getattr(p, key)


@bp.route("/")
def dashboard():
    args = request.args
    view = args.get("view") if args.get("view") in ("cards", "table") else "cards"
    q = args.get("q", "").strip()
    ptype = args.get("type", "") if args.get("type") in PROJECT_TYPES else ""
    stale_only = args.get("status") == "stale"
    status = args.get("status", "") if args.get("status") in STATUSES else ""
    sort = args.get("sort") if args.get("sort") in TABLE_SORTS else "updated_at"
    direction = args.get("dir") if args.get("dir") in ("asc", "desc") else DEFAULT_DIR.get(sort, "asc")

    query = Project.query.filter(Project.archived_at.is_(None))
    if ptype:
        query = query.filter(Project.type == ptype)
    if status:
        query = query.filter(Project.status == status)
    if q:
        like = f"%{q}%"
        note_match = (db.session.query(ProgressLog.project_id)
                      .filter(ProgressLog.archived_at.is_(None), ProgressLog.note.ilike(like)))
        query = query.filter(or_(Project.title.ilike(like), Project.summary.ilike(like),
                                 Project.id.in_(note_match)))
    projects = query.all()
    stale = activity.staleness(projects)
    if stale_only:
        projects = [p for p in projects if stale[p.id]["stale"]]
    # Secondary order: most recently updated first, then the chosen key.
    projects.sort(key=lambda p: p.updated_at, reverse=True)
    projects.sort(key=_sort_key(sort), reverse=direction == "desc")

    state = {"view": view, "q": q, "type": ptype, "status": "stale" if stale_only else status,
             "sort": sort, "dir": direction}
    return render_template(
        "dashboard.html", projects=projects, state=state, sorts=SORTS, default_dir=DEFAULT_DIR, stale=stale,
        columns=[status] if status else STATUSES,
    )


@bp.route("/projects/<int:project_id>")
def detail(project_id):
    project = get_project(project_id)
    history = (AuditLog.query.filter_by(project_id=project.id)
               .order_by(AuditLog.ts.desc(), AuditLog.id.desc()).all())
    # Notes written through MCP get a "Claude" label (source comes from the audit log).
    note_sources = {a.entity_id: a.source for a in history if a.entity == "note" and a.action == "create"}
    return render_template(
        "project_detail.html", project=project, history=history, note_sources=note_sources,
        stale=activity.staleness([project])[project.id],
        editing_note=request.args.get("edit_note", type=int),
    )


def _render_form(project, values, status_code=200):
    return render_template("project_form.html", project=project, values=values), status_code


# Unlinked fallback for adding a project by hand; projects normally arrive via MCP.
@bp.route("/projects/new", methods=["GET", "POST"])
def new_project():
    if request.method == "POST":
        try:
            project = services.create_project(actor(), request.form)
        except ValidationError as e:
            flash(str(e), "error")
            return _render_form(None, request.form, 400)
        db.session.commit()
        flash("Project created.")
        return redirect(url_for("projects.detail", project_id=project.id))
    return _render_form(None, {"type": "personal", "status": "idea"})


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def edit_project(project_id):
    project = get_live(Project, project_id)
    if request.method == "POST":
        try:
            changes = services.update_project(actor(), project, request.form)
        except ValidationError as e:
            flash(str(e), "error")
            return _render_form(project, request.form, 400)
        db.session.commit()
        flash("Project saved." if changes else "No changes.")
        return redirect(url_for("projects.detail", project_id=project.id))
    return _render_form(project, {f: getattr(project, f) or "" for f in services.PROJECT_FIELDS})


@bp.route("/projects/<int:project_id>/archive", methods=["POST"])
def archive_project(project_id):
    project = get_live(Project, project_id)
    services.archive_project(actor(), project)
    db.session.commit()
    flash(f"Archived “{project.title}”.")
    return redirect(url_for("projects.dashboard"))


def _back(project_id):
    return redirect(url_for("projects.detail", project_id=project_id))


def _apply(fn, ok_message):
    """Run a service call; flash its validation error or commit and flash success."""
    try:
        result = fn()
    except (ValidationError, ArchivedError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return None
    db.session.commit()
    if ok_message:
        flash(ok_message)
    return result


@bp.route("/projects/<int:project_id>/notes", methods=["POST"])
def add_note(project_id):
    project = get_live(Project, project_id)
    _apply(lambda: services.add_note(
        actor(), project, request.form.get("note"), request.form.get("date")), "Note added.")
    return _back(project.id)


@bp.route("/notes/<int:note_id>", methods=["POST"])
def update_note(note_id):
    log = get_live(ProgressLog, note_id)
    changes = _apply(lambda: services.update_note(
        actor(), log, request.form.get("note"), request.form.get("date")), None)
    if changes:
        flash("Note updated.")
    return _back(log.project_id)


@bp.route("/notes/<int:note_id>/archive", methods=["POST"])
def archive_note(note_id):
    log = get_live(ProgressLog, note_id)
    _apply(lambda: services.archive_note(actor(), log), "Note archived.")
    return _back(log.project_id)


@bp.route("/projects/<int:project_id>/milestones", methods=["POST"])
def add_milestone(project_id):
    project = get_live(Project, project_id)
    _apply(lambda: services.add_milestone(actor(), project, request.form.get("title")),
           "Milestone added.")
    return _back(project.id)


@bp.route("/milestones/<int:milestone_id>/toggle", methods=["POST"])
def toggle_milestone(milestone_id):
    milestone = get_live(Milestone, milestone_id)
    done = request.form.get("done") == "1"
    new_status = _apply(lambda: services.set_milestone_done(actor(), milestone, done), None)
    if new_status == "done":
        flash("All milestones done: moved to Done.")
    elif new_status:
        flash(f"Milestone reopened: moved back to {new_status.capitalize()}.")
    return _back(milestone.project_id)


@bp.route("/milestones/<int:milestone_id>/archive", methods=["POST"])
def archive_milestone(milestone_id):
    milestone = get_live(Milestone, milestone_id)
    _apply(lambda: services.archive_milestone(actor(), milestone),
           "Milestone archived.")
    return _back(milestone.project_id)


@bp.route("/projects/<int:project_id>/snooze", methods=["POST"])
def snooze_project(project_id):
    project = get_live(Project, project_id)
    _apply(lambda: services.snooze_project(actor(), project), "Got it. The stale clock is reset.")
    return _back(project.id)


@bp.route("/projects/<int:project_id>/unarchive", methods=["POST"])
def unarchive_project(project_id):
    project = get_project(project_id)
    _apply(lambda: services.unarchive_project(actor(), project), f"Restored “{project.title}”.")
    return _back(project.id)


@bp.route("/notes/<int:note_id>/unarchive", methods=["POST"])
def unarchive_note(note_id):
    log = db.session.get(ProgressLog, note_id) or abort(404)
    _apply(lambda: services.unarchive_note(actor(), log), "Note restored.")
    return _back(log.project_id)


@bp.route("/milestones/<int:milestone_id>/unarchive", methods=["POST"])
def unarchive_milestone(milestone_id):
    milestone = db.session.get(Milestone, milestone_id) or abort(404)
    _apply(lambda: services.unarchive_milestone(actor(), milestone), "Milestone restored.")
    return _back(milestone.project_id)


@bp.route("/archive")
def archive():
    projects = (Project.query.filter(Project.archived_at.isnot(None))
                .order_by(Project.archived_at.desc()).all())
    notes = (ProgressLog.query.join(Project)
             .filter(ProgressLog.archived_at.isnot(None), Project.archived_at.is_(None))
             .order_by(ProgressLog.archived_at.desc()).all())
    milestones = (Milestone.query.join(Project)
                  .filter(Milestone.archived_at.isnot(None), Project.archived_at.is_(None))
                  .order_by(Milestone.archived_at.desc()).all())
    return render_template("archive.html", projects=projects, notes=notes, milestones=milestones)


@bp.route("/audit")
def audit():
    page = request.args.get("page", 1, type=int)
    rows = (AuditLog.query.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .paginate(page=page, per_page=100, error_out=False))
    return render_template("audit.html", rows=rows)
