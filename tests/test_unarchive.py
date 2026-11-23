import pytest

from tracker import mcp_server, services
from tracker.models import AuditLog, db


def test_unarchive_project_from_web(auth, make_project, actor):
    p = make_project(title="Back again")
    services.archive_project(actor, p)
    db.session.commit()
    html = auth.get("/archive").get_data(as_text=True)
    assert "Restore</button>" in html
    r = auth.post(f"/projects/{p.id}/unarchive", follow_redirects=True)
    assert "Restored “Back again”" in r.get_data(as_text=True)
    db.session.refresh(p)
    assert p.archived_at is None
    assert "Back again" in auth.get("/").get_data(as_text=True)
    # Editable again.
    assert auth.get(f"/projects/{p.id}/edit").status_code == 200
    row = AuditLog.query.filter_by(entity="project", action="unarchive").one()
    assert (row.user, row.source) == ("me", "web")


def test_unarchiving_project_keeps_individually_archived_items_archived(make_project, actor):
    p = make_project()
    kept = services.add_note(actor, p, "visible")
    hidden = services.add_note(actor, p, "hidden")
    services.archive_note(actor, hidden)
    services.archive_project(actor, p)
    services.unarchive_project(actor, p)
    db.session.commit()
    assert [n.note for n in p.active_logs] == ["visible"]
    assert hidden.archived_at is not None and kept.archived_at is None


def test_unarchive_note_and_milestone_from_web(auth, make_project, actor):
    p = make_project(status="active")
    note = services.add_note(actor, p, "restore me")
    ms = services.add_milestone(actor, p, "restore me too")
    services.archive_note(actor, note)
    services.archive_milestone(actor, ms)
    db.session.commit()
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Archived notes (1)" in html and "Archived milestones (1)" in html
    auth.post(f"/notes/{note.id}/unarchive")
    auth.post(f"/milestones/{ms.id}/unarchive")
    db.session.refresh(note)
    db.session.refresh(ms)
    assert note.archived_at is None and ms.archived_at is None
    db.session.refresh(p)
    assert p.progress == (0, 1, 0)
    assert AuditLog.query.filter_by(action="unarchive").count() == 2


def test_cannot_unarchive_items_of_an_archived_project(auth, make_project, actor):
    p = make_project()
    note = services.add_note(actor, p, "n")
    services.archive_note(actor, note)
    services.archive_project(actor, p)
    db.session.commit()
    r = auth.post(f"/notes/{note.id}/unarchive", follow_redirects=True)
    assert b"read-only" in r.data
    db.session.refresh(note)
    assert note.archived_at is not None


def test_unarchive_something_not_archived_is_rejected(auth, make_project):
    p = make_project()
    r = auth.post(f"/projects/{p.id}/unarchive", follow_redirects=True)
    assert b"isn&#39;t archived" in r.data
    assert AuditLog.query.filter_by(action="unarchive").count() == 0


def test_unarchive_routes_404(auth):
    for url in ["/projects/999/unarchive", "/notes/999/unarchive", "/milestones/999/unarchive"]:
        assert auth.post(url).status_code == 404


def test_confirm_text_says_restorable(auth, make_project):
    p = make_project()
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "restore it later from the Archive" in html and "can&#39;t be undone" not in html


@pytest.fixture
def mcp_app(app, monkeypatch):
    monkeypatch.setattr(mcp_server, "app", app)


def test_mcp_unarchive_tools(mcp_app, make_project):
    p = make_project(title="Revived")
    note = mcp_server.add_note(p.id, "n")
    ms = mcp_server.add_milestones(p.id, ["m"])["milestones"][0]
    mcp_server.archive_note(note["id"])
    mcp_server.archive_milestone(ms["id"])
    detail = mcp_server.get_project(p.id)
    assert [n["id"] for n in detail["archived_notes"]] == [note["id"]]
    assert [m["id"] for m in detail["archived_milestones"]] == [ms["id"]]
    mcp_server.unarchive_note(note["id"])
    assert mcp_server.unarchive_milestone(ms["id"])["progress"] == {"done": 0, "total": 1, "percent": 0}
    mcp_server.archive_project(p.id)
    assert mcp_server.list_projects() == []
    assert mcp_server.unarchive_project(p.id)["archived"] is False
    assert [x["title"] for x in mcp_server.list_projects()] == ["Revived"]
    rows = AuditLog.query.filter_by(action="unarchive", source="mcp").count()
    assert rows == 3
