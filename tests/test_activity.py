from datetime import timedelta

from tracker import activity, services
from tracker.models import db, utcnow


def types(project_id=None):
    return [e.type for e in activity.events(project_id=project_id)]


def test_every_kind_of_change_becomes_a_named_event(make_project, actor):
    p = make_project(status="idea")
    services.update_project(actor, p, {"status": "active"})
    services.update_project(actor, p, {"summary": "more detail"})
    n = services.add_note(actor, p, "note")
    services.update_note(actor, n, "note!", n.date)
    m = services.add_milestone(actor, p, "only one")
    services.set_milestone_done(actor, m, True)  # also moves the project to done
    services.set_milestone_done(actor, m, False)  # and back
    services.archive_note(actor, n)
    services.unarchive_note(actor, n)
    services.archive_milestone(actor, m)
    services.archive_project(actor, p)
    services.unarchive_project(actor, p)
    db.session.commit()
    assert types(p.id) == [
        "created", "status_changed", "project_edited", "note_added", "note_edited",
        "milestone_added", "milestone_done", "status_changed", "milestone_reopened",
        "status_changed", "note_archived", "note_unarchived", "milestone_archived",
        "archived", "unarchived",
    ]
    changes = [(e.from_value, e.to_value) for e in activity.events(project_id=p.id)
               if e.type == "status_changed"]
    assert changes == [("idea", "active"), ("active", "done"), ("done", "active")]


def test_event_details_and_source(make_project):
    p = make_project()
    services.add_milestone(services.Actor("claude", "mcp"), p, "Ship it")
    db.session.commit()
    ev = activity.events(project_id=p.id)[-1]
    assert (ev.type, ev.to_value, ev.source) == ("milestone_added", "Ship it", "mcp")


def test_events_time_window_and_last_activity(make_project, actor):
    a, b = make_project(title="A"), make_project(title="B")
    services.add_note(actor, b, "later")
    db.session.commit()
    now = utcnow()
    assert len(activity.events(start=now - timedelta(minutes=1))) == 3
    assert activity.events(end=now - timedelta(days=1)) == []
    last = activity.last_activity()
    assert set(last) == {a.id, b.id} and last[b.id] >= last[a.id]
