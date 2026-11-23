from datetime import date, datetime

import pytest

from tracker import reviews, services
from tracker.models import AuditLog, Review, db

TODAY = date(2026, 10, 28)  # a Wednesday


def at(utc_string):
    return datetime.fromisoformat(utc_string)


def stamp(rows, utc_string):
    for r in rows:
        r.ts = at(utc_string)
    db.session.commit()


def new_rows(before):
    return AuditLog.query.filter(AuditLog.id > before).all()


def last_id():
    return db.session.query(db.func.max(AuditLog.id)).scalar() or 0


def test_period_bounds():
    assert reviews.period_bounds("weekly", 0, TODAY) == (date(2026, 10, 26), date(2026, 11, 2))
    assert reviews.period_bounds("weekly", -1, TODAY) == (date(2026, 10, 19), date(2026, 10, 26))
    assert reviews.period_bounds("monthly", 0, TODAY) == (date(2026, 10, 1), date(2026, 11, 1))
    assert reviews.period_bounds("monthly", -10, TODAY) == (date(2025, 12, 1), date(2026, 1, 1))
    assert reviews.period_bounds("yearly", -1, TODAY) == (date(2025, 1, 1), date(2026, 1, 1))
    assert reviews.period_label("weekly", date(2026, 10, 26), date(2026, 11, 2)) == "Oct 26 – Nov 1, 2026"
    with pytest.raises(ValueError):
        reviews.period_bounds("daily", 0, TODAY)


def test_sunday_night_pacific_belongs_to_that_week(make_project, actor):
    p = make_project()
    stamp(new_rows(0), "2026-10-20T12:00:00")
    before = last_id()
    services.add_note(actor, p, "late Sunday note")
    db.session.commit()
    # Sunday Oct 25, 11pm PDT == Monday Oct 26, 06:00 UTC.
    stamp(new_rows(before), "2026-10-26T06:00:00")
    last_week = reviews.compute("weekly", -1, TODAY)
    this_week = reviews.compute("weekly", 0, TODAY)
    assert last_week["totals"]["notes_added"] == 1
    assert this_week["totals"]["notes_added"] == 0


def seed_month(make_project, actor):
    """October 2026: one idea activated, one project finished via milestones."""
    a = make_project(title="Alpha", status="idea", type="software")
    b = make_project(title="Bravo", status="active", type="DIY")
    stamp(new_rows(0), "2026-09-15T12:00:00")
    before = last_id()
    services.update_project(actor, a, {"status": "active"})
    services.add_note(actor, a, "started")
    services.add_note(actor, b, "progress")
    m1, m2 = services.add_milestone(actor, b, "one"), services.add_milestone(actor, b, "two")
    services.set_milestone_done(actor, m1, True)
    services.set_milestone_done(actor, m2, True)  # -> done
    db.session.commit()
    stamp(new_rows(before), "2026-10-10T18:00:00")
    return a, b


def test_monthly_counts_and_lists(make_project, actor):
    seed_month(make_project, actor)
    data = reviews.compute("monthly", 0, TODAY)
    t = data["totals"]
    assert (t["projects_touched"], t["notes_added"], t["milestones_completed"]) == (2, 2, 2)
    assert (t["ideas_activated"], t["projects_completed"], t["status_changes"]) == (1, 1, 2)
    assert data["ideas_activated"] == ["Alpha"] and data["projects_completed"] == ["Bravo"]
    assert data["most_active"]["title"] == "Bravo"
    assert {x["type"] for x in data["by_type"]} == {"software", "DIY"}
    assert data["previous_totals"]["projects_started"] == 2  # both created in September
    assert data["totals"]["projects_started"] == 0


def test_reopened_milestone_not_counted_as_completed(make_project, actor):
    p = make_project(status="active")
    m = services.add_milestone(actor, p, "flip-flop")
    services.add_milestone(actor, p, "other")
    services.set_milestone_done(actor, m, True)
    services.set_milestone_done(actor, m, False)
    db.session.commit()
    data = reviews.compute("weekly")
    assert data["totals"]["milestones_completed"] == 0


def test_yearly_extras(make_project, actor):
    seed_month(make_project, actor)
    data = reviews.compute("yearly", 0, TODAY)
    assert data["most_active_month"]["month"] == "October"
    assert data["avg_days_idea_to_active"] == 25.2  # Sep 15 noon -> Oct 10 18:00 UTC
    assert data["avg_days_active_to_done"] == 25.2
    assert data["longest_running_active"]["title"] == "Alpha"
    days = [d for d in data["heatmap"] if d["in_year"]]
    assert len(days) == 365 and data["heatmap"][0]["date"] <= "2026-01-01"
    assert sum(d["events"] for d in days) == len(AuditLog.query.all())


def test_reflections_save_reload_and_are_unique(app):
    prompt = reviews.prompts("weekly")[0]
    reviews.save_reflection("weekly", 0, {prompt: "Shipped reviews"}, TODAY)
    reviews.save_reflection("weekly", 0, {prompt: "Shipped reviews!"}, TODAY)
    assert Review.query.count() == 1
    assert reviews.get_reflection("weekly", date(2026, 10, 26)) == {prompt: "Shipped reviews!"}
    with pytest.raises(ValueError, match="Unknown prompt"):
        reviews.save_reflection("weekly", 0, {"made up?": "x"}, TODAY)


def test_reviews_pages(auth):
    for kind in ("weekly", "monthly", "yearly"):
        html = auth.get(f"/reviews/{kind}").get_data(as_text=True)
        assert "Save reflection" in html and "Projects touched" in html
    assert auth.get("/reviews/").status_code == 302
    assert auth.get("/reviews/daily").status_code == 404
    assert 'class="heatmap"' in auth.get("/reviews/yearly").get_data(as_text=True)


def test_reflection_form_saves(auth):
    r = auth.post("/reviews/weekly/reflection?offset=-1",
                  {"answer-0": "Lots", "answer-1": "", "answer-2": "Reviews"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Reflection saved" in html and "Lots" in html
    review = Review.query.one()
    assert list(review.answers.values()) == ["Lots", "Reviews"]


def test_review_pages_need_login(client):
    assert client.get("/reviews/weekly").status_code == 302
