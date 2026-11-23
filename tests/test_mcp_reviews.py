from datetime import date, datetime

import pytest

from tracker import mcp_server, reviews, services
from tracker.models import AuditLog, Review, db

TODAY = date(2026, 10, 28)


@pytest.fixture(autouse=True)
def mcp_app(app, monkeypatch):
    monkeypatch.setattr(mcp_server, "app", app)
    monkeypatch.setattr(reviews, "local_today", lambda: TODAY)


def test_get_review_matches_web_stats(make_project, actor):
    p = make_project(status="idea")
    services.update_project(actor, p, {"status": "active"})
    services.add_note(actor, p, "kicked off")
    # Pin the events inside TODAY's week, so the test doesn't depend on the real date.
    AuditLog.query.update({"ts": datetime(2026, 10, 28, 18, 0)})
    db.session.commit()  # the tool reads through its own session
    for kind in ("weekly", "monthly", "yearly"):
        assert mcp_server.get_review(kind, 0) == reviews.review(kind, 0)
    data = mcp_server.get_review("weekly")
    assert data["period"]["label"] == "Oct 26 – Nov 1, 2026"
    assert data["totals"]["notes_added"] == 1 and data["totals"]["status_changes"] == 1
    assert len(data["reflection"]["prompts"]) == 3


def test_save_reflection_round_trip():
    prompt = mcp_server.get_review("monthly")["reflection"]["prompts"][1]
    saved = mcp_server.save_review_reflection("monthly", 0, {prompt: "Nothing to drop"})
    assert saved["answers"] == {prompt: "Nothing to drop"}
    assert saved["period"]["label"] == "October 2026"
    assert mcp_server.get_review("monthly")["reflection"]["answers"] == {prompt: "Nothing to drop"}
    # Past period, separate row.
    mcp_server.save_review_reflection("monthly", -1, {prompt: "September answer"})
    assert Review.query.count() == 2


def test_bad_inputs():
    with pytest.raises(ValueError, match="offset"):
        mcp_server.get_review("weekly", 1)
    with pytest.raises(ValueError, match="period must be"):
        mcp_server.get_review("daily")
    with pytest.raises(ValueError, match="Unknown prompt"):
        mcp_server.save_review_reflection("weekly", 0, {"How was it?": "fine"})
