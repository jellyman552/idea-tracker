from tracker import services
from tracker.models import AuditLog, Project, db


def audit_rows(**filters):
    return AuditLog.query.filter_by(**filters).order_by(AuditLog.id).all()


def test_detail_and_404(auth, make_project):
    p = make_project(title="Visible", chat_link="https://claude.ai/chat/abc")
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Visible" in html and "https://claude.ai/chat/abc" in html
    assert auth.get("/projects/999").status_code == 404


def test_edit_project_records_diff(auth, make_project):
    p = make_project(title="Before", status="active")
    r = auth.post(f"/projects/{p.id}/edit", {
        "title": "After", "type": "software", "status": "paused", "summary": "", "chat_link": ""})
    assert r.status_code == 302
    db.session.refresh(p)
    assert (p.title, p.status) == ("After", "paused")
    row = audit_rows(entity="project", action="update")[-1]
    assert row.changes == {"title": ["Before", "After"], "status": ["active", "paused"]}
    assert (row.user, row.source) == ("me", "web")


def test_edit_without_changes_is_not_audited(auth, make_project):
    p = make_project(title="Same")
    before = AuditLog.query.count()
    auth.post(f"/projects/{p.id}/edit", {
        "title": "Same", "type": "software", "status": "active", "summary": "", "chat_link": ""})
    assert AuditLog.query.count() == before


def test_edit_validation(auth, make_project):
    p = make_project(title="Valid")
    base = {"title": "Valid", "type": "software", "status": "active", "summary": "", "chat_link": ""}
    for bad, message in [
        ({"title": "  "}, b"Title is required"),
        ({"type": "hobby"}, b"Type must be one of"),
        ({"status": "stalled"}, b"Status must be one of"),
        ({"chat_link": "javascript:alert(1)"}, b"https://"),
    ]:
        r = auth.post(f"/projects/{p.id}/edit", {**base, **bad})
        assert r.status_code == 400
        assert message in r.data
    db.session.refresh(p)
    assert p.title == "Valid"


def test_new_project_fallback_route(auth):
    r = auth.post("/projects/new", {"title": "Manual", "type": "DIY", "status": "idea", "summary": "", "chat_link": ""})
    assert r.status_code == 302
    p = Project.query.filter_by(title="Manual").one()
    assert audit_rows(entity="project", action="create", entity_id=p.id)


def test_no_new_project_button_in_nav(auth):
    assert "New project" not in auth.get("/").get_data(as_text=True)


def test_archived_project_is_read_only(auth, make_project):
    p = make_project(title="Retire me")
    auth.post(f"/projects/{p.id}/archive")
    db.session.refresh(p)
    assert p.archived_at is not None
    assert db.session.get(Project, p.id)  # never deleted
    assert auth.get(f"/projects/{p.id}/edit").status_code == 409
    assert auth.post(f"/projects/{p.id}/notes", {"note": "x", "date": "2026-01-01"}).status_code == 409
    assert auth.post(f"/projects/{p.id}/milestones", {"title": "x"}).status_code == 409
    assert auth.post(f"/projects/{p.id}/archive").status_code == 409
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "read-only" in html and "Edit</a>" not in html
    assert "Retire me" in auth.get("/archive").get_data(as_text=True)
    assert audit_rows(entity="project", action="archive")


def test_there_is_no_delete_route(auth, make_project):
    p = make_project()
    assert auth.post(f"/projects/{p.id}/delete").status_code == 404


def test_audit_page_lists_changes(auth, make_project, actor):
    p = make_project(title="Audited")
    services.add_note(actor, p, "first note")
    db.session.commit()
    html = auth.get("/audit").get_data(as_text=True)
    assert "create project" in html and "create note" in html and "first note" in html


def test_history_on_project_page(auth, make_project):
    p = make_project(title="Historic")
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "History (1)" in html
