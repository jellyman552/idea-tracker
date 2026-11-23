from datetime import timedelta

from tracker import services
from tracker.models import db, utcnow


def titles_in_order(html, titles):
    return sorted(titles, key=html.index)


def test_defaults_to_cards_sorted_by_last_updated(auth, make_project):
    old = make_project(title="Old one")
    old.updated_at = utcnow() - timedelta(days=5)
    make_project(title="New one")
    db.session.commit()
    html = auth.get("/").get_data(as_text=True)
    assert 'class="board"' in html
    for heading in ["Idea", "Active", "Paused", "Done"]:
        assert f"<h3>{heading}</h3>" in html
    assert titles_in_order(html, ["Old one", "New one"]) == ["New one", "Old one"]


def test_table_view_and_sorting(auth, make_project):
    make_project(title="Bravo")
    make_project(title="Alpha")
    html = auth.get("/?view=table&sort=title&dir=asc").get_data(as_text=True)
    assert "<table" in html
    assert titles_in_order(html, ["Alpha", "Bravo"]) == ["Alpha", "Bravo"]
    html = auth.get("/?view=table&sort=title&dir=desc").get_data(as_text=True)
    assert titles_in_order(html, ["Alpha", "Bravo"]) == ["Bravo", "Alpha"]


def test_sort_by_progress(auth, make_project, actor):
    low, high = make_project(title="Low progress"), make_project(title="High progress")
    for p, ticked in [(low, 0), (high, 2)]:
        for i in range(3):
            m = services.add_milestone(actor, p, f"m{i}")
            if i < ticked:
                m.done = True
    db.session.commit()
    html = auth.get("/?view=table&sort=progress").get_data(as_text=True)
    assert titles_in_order(html, ["Low progress", "High progress"]) == ["High progress", "Low progress"]


def test_filters(auth, make_project):
    make_project(title="Biz idea", type="business", status="idea")
    make_project(title="Soft active", type="software", status="active")
    html = auth.get("/?type=business").get_data(as_text=True)
    assert "Biz idea" in html and "Soft active" not in html
    html = auth.get("/?status=active").get_data(as_text=True)
    assert "Soft active" in html and "Biz idea" not in html
    assert "<h3>Idea</h3>" not in html  # only the filtered column is shown


def test_search_matches_title_summary_and_live_notes(auth, make_project, actor):
    make_project(title="Herb garden")
    make_project(title="Other", summary="all about spuds")
    noted = make_project(title="Noted")
    services.add_note(actor, noted, "bought fertiliser")
    hidden = make_project(title="Hidden")
    services.archive_note(actor, services.add_note(actor, hidden, "secret fertiliser"))
    db.session.commit()
    assert "Herb garden" in auth.get("/?q=herb").get_data(as_text=True)
    assert "Other" in auth.get("/?q=SPUDS").get_data(as_text=True)
    html = auth.get("/?q=fertiliser").get_data(as_text=True)
    assert "Noted" in html and "Hidden" not in html


def test_archived_projects_are_not_listed(auth, make_project, actor):
    p = make_project(title="Gone")
    services.archive_project(actor, p)
    db.session.commit()
    assert "Gone" not in auth.get("/").get_data(as_text=True)


def test_bad_query_params_fall_back_to_defaults(auth, make_project):
    make_project()
    r = auth.get("/?view=x&type=nope&status=nope&sort=nope&dir=sideways")
    assert r.status_code == 200
    assert 'class="board"' in r.get_data(as_text=True)


def test_progress_bar_on_cards(auth, make_project, actor):
    p = make_project(title="Has milestones")
    for i in range(5):
        m = services.add_milestone(actor, p, f"step {i}")
        m.done = i < 3
    make_project(title="Just an idea", status="idea")
    db.session.commit()
    html = auth.get("/").get_data(as_text=True)
    assert "3 of 5" in html
    assert html.count('class="pbar "') == 1  # the idea gets no bar


def test_sort_direction_button_only_in_cards_view(auth, make_project):
    make_project()
    assert "Toggle sort direction" in auth.get("/").get_data(as_text=True)
    assert "Toggle sort direction" not in auth.get("/?view=table").get_data(as_text=True)


def test_results_container_for_live_search(auth, make_project):
    make_project(title="Findable")
    html = auth.get("/?q=find").get_data(as_text=True)
    assert 'id="results"' in html and 'data-live' in html and "Findable" in html
