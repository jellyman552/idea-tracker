from datetime import timedelta

import pytest

from tracker import activity, mcp_server, services
from tracker.models import AuditLog, db, utcnow


def age(project, days):
    """Pretend everything about the project happened `days` ago."""
    then = utcnow() - timedelta(days=days)
    project.created_at = then
    for row in AuditLog.query.filter_by(project_id=project.id):
        row.ts = then
    db.session.commit()


@pytest.mark.parametrize("status, days, stale", [
    ("active", 13, False), ("active", 14, True),
    ("paused", 14, True),
    ("idea", 59, False), ("idea", 60, True),
    ("done", 400, False),
])
def test_thresholds_per_status(make_project, status, days, stale):
    p = make_project(status=status)
    age(p, days)
    info = activity.staleness([p])[p.id]
    assert info["stale"] is stale and info["days"] == days


def test_archived_projects_never_stale(make_project, actor):
    p = make_project(status="active")
    services.archive_project(actor, p)
    db.session.commit()
    age(p, 100)
    assert activity.staleness([p])[p.id]["stale"] is False


def test_any_activity_resets_the_clock(make_project, actor):
    p = make_project(status="active")
    age(p, 30)
    services.add_note(actor, p, "still going")
    db.session.commit()
    assert activity.staleness([p])[p.id]["stale"] is False


def test_thresholds_come_from_config(app, make_project):
    p = make_project(status="active")
    age(p, 5)
    app.config["STALE_AFTER_DAYS"] = {"active": 3}
    assert activity.staleness([p])[p.id]["stale"] is True


def test_dashboard_badge_and_filter(auth, make_project):
    quiet, busy = make_project(title="Quiet one"), make_project(title="Busy one")
    age(quiet, 20)
    html = auth.get("/").get_data(as_text=True)
    assert "stale · 20d" in html
    html = auth.get("/?status=stale").get_data(as_text=True)
    assert "Quiet one" in html and "Busy one" not in html
    assert 'value="stale" selected' in html
    assert "stale · 20d" in auth.get("/?view=table").get_data(as_text=True)


def test_snooze_from_project_page(auth, make_project):
    p = make_project(title="Keep me")
    age(p, 30)
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Quiet for 30 days" in html and "Keep it" in html
    before = p.updated_at
    r = auth.post(f"/projects/{p.id}/snooze", follow_redirects=True)
    assert "stale clock is reset" in r.get_data(as_text=True)
    assert "Quiet for" not in r.get_data(as_text=True)
    db.session.refresh(p)
    assert p.updated_at == before  # snoozing isn't an edit
    assert [e.type for e in activity.events(project_id=p.id)][-1] == "snoozed"


def test_mcp_stale_tools(app, monkeypatch, make_project):
    monkeypatch.setattr(mcp_server, "app", app)
    old, older = make_project(title="Old"), make_project(title="Older")
    make_project(title="Fresh")
    age(old, 20)
    age(older, 40)
    result = mcp_server.get_stale_projects()
    assert [(r["title"], r["days_since_activity"]) for r in result] == [("Older", 40), ("Old", 20)]
    mcp_server.snooze_project(older.id)
    assert [r["title"] for r in mcp_server.get_stale_projects()] == ["Old"]
    assert AuditLog.query.filter_by(action="snooze", source="mcp").count() == 1
