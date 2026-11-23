from datetime import date

from tracker import services
from tracker.models import AuditLog, Milestone, ProgressLog, db


def test_add_note_with_custom_date(auth, make_project):
    p = make_project()
    r = auth.post(f"/projects/{p.id}/notes", {"note": "Backdated", "date": "2026-01-15"})
    assert r.status_code == 302
    log = ProgressLog.query.one()
    assert (log.note, log.date) == ("Backdated", date(2026, 1, 15))


def test_add_note_validation(auth, make_project):
    p = make_project()
    r = auth.post(f"/projects/{p.id}/notes", {"note": "  ", "date": "2026-01-15"}, follow_redirects=True)
    assert b"can&#39;t be empty" in r.data
    r = auth.post(f"/projects/{p.id}/notes", {"note": "ok", "date": "not-a-date"}, follow_redirects=True)
    assert b"YYYY-MM-DD" in r.data
    assert ProgressLog.query.count() == 0


def test_adding_note_bumps_updated_at(auth, make_project):
    p = make_project()
    before = p.updated_at
    auth.post(f"/projects/{p.id}/notes", {"note": "progress", "date": "2026-01-15"})
    db.session.refresh(p)
    assert p.updated_at >= before


def test_edit_note(auth, make_project, actor):
    p = make_project()
    log = services.add_note(actor, p, "typo", "2026-02-01")
    db.session.commit()
    assert b"Save</button>" in auth.get(f"/projects/{p.id}?edit_note={log.id}").data
    auth.post(f"/notes/{log.id}", {"note": "fixed", "date": "2026-02-02"})
    db.session.refresh(log)
    assert (log.note, log.date) == ("fixed", date(2026, 2, 2))
    row = AuditLog.query.filter_by(entity="note", action="update").one()
    assert row.changes == {"note": ["typo", "fixed"], "date": ["2026-02-01", "2026-02-02"]}


def test_archive_note(auth, make_project, actor):
    p = make_project()
    log = services.add_note(actor, p, "old news")
    db.session.commit()
    auth.post(f"/notes/{log.id}/archive")
    db.session.refresh(log)
    assert log.archived_at is not None
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Archived notes (1)" in html
    assert auth.post(f"/notes/{log.id}", {"note": "edit", "date": "2026-01-01"}).status_code == 409
    assert "old news" in auth.get("/archive").get_data(as_text=True)


def add_milestones(actor, project, n, done=0):
    ms = [services.add_milestone(actor, project, f"Milestone {i}") for i in range(n)]
    for m in ms[:done]:
        m.done = True
    db.session.commit()
    return ms


def test_progress_rules(make_project, actor):
    p = make_project(status="active")
    assert p.progress is None  # no milestones yet
    add_milestones(actor, p, 5, done=3)
    assert p.progress == (3, 5, 60)
    p.status = "idea"
    assert p.progress is None
    p.status = "done"
    assert p.progress == (5, 5, 100)


def test_archived_milestones_do_not_count(auth, make_project, actor):
    p = make_project()
    ms = add_milestones(actor, p, 4, done=1)
    auth.post(f"/milestones/{ms[3].id}/archive")
    db.session.refresh(p)
    assert p.progress == (1, 3, 33)


def test_ticking_last_milestone_moves_project_to_done(auth, make_project, actor):
    p = make_project(status="active")
    ms = add_milestones(actor, p, 2, done=1)
    r = auth.post(f"/milestones/{ms[1].id}/toggle", {"done": "1"}, follow_redirects=True)
    assert b"moved to Done" in r.data
    db.session.refresh(p)
    assert p.status == "done"
    status_change = AuditLog.query.filter_by(entity="project", action="update").one()
    assert status_change.changes == {"status": ["active", "done"]}


def test_unticking_moves_project_back_to_previous_status(auth, make_project, actor):
    p = make_project(status="paused")
    ms = add_milestones(actor, p, 1)
    auth.post(f"/milestones/{ms[0].id}/toggle", {"done": "1"})
    db.session.refresh(p)
    assert p.status == "done"
    r = auth.post(f"/milestones/{ms[0].id}/toggle", {"done": "0"}, follow_redirects=True)
    assert b"moved back to Paused" in r.data
    db.session.refresh(p)
    assert p.status == "paused"


def test_unticking_on_manually_done_project_defaults_to_active(make_project, actor):
    p = make_project(status="done")
    ms = add_milestones(actor, p, 1, done=1)
    assert services.set_milestone_done(actor, ms[0], False) == "active"


def test_toggle_is_audited(auth, make_project, actor):
    p = make_project()
    ms = add_milestones(actor, p, 2)
    auth.post(f"/milestones/{ms[0].id}/toggle", {"done": "1"})
    row = AuditLog.query.filter_by(entity="milestone", action="update").one()
    assert row.changes["done"] == [False, True]


def test_add_milestone_validation(auth, make_project):
    p = make_project()
    auth.post(f"/projects/{p.id}/milestones", {"title": "   "})
    assert Milestone.query.count() == 0
    auth.post(f"/projects/{p.id}/milestones", {"title": "Real one"})
    assert Milestone.query.one().title == "Real one"


def test_services_record_source(make_project):
    p = services.create_project(services.Actor(user="claude", source="mcp"), {"title": "From chat"})
    db.session.commit()
    row = AuditLog.query.filter_by(entity_id=p.id, entity="project").one()
    assert (row.user, row.source) == ("claude", "mcp")
