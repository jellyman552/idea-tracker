import urllib.error
from urllib.parse import parse_qs, urlparse
from datetime import timedelta

import pytest

from tracker import activity, github_sync, mcp_server, reviews, services
from tracker.models import AuditLog, Commit, db, utcnow
from tracker.services import ValidationError


def gh_commit(sha, message="Did a thing", when=None):
    when = (when or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}",
            "commit": {"message": message, "author": {"name": "Alex", "date": when},
                       "committer": {"name": "GitHub", "date": when}}}


class FakeGitHub:
    def __init__(self, repos):
        self.repos = repos  # {"owner/repo": [commits] | HTTP status int}
        self.calls = []

    def __call__(self, url, token):
        self.calls.append(url)
        path = url.split("/repos/")[1].split("/commits")[0]
        data = self.repos[path]
        if isinstance(data, int):
            raise urllib.error.HTTPError(url, data, "error", {}, None)
        return data


def test_repo_url_validation_and_normalising(make_project, actor):
    p = make_project()
    services.update_project(actor, p, {"repo_url": "https://github.com/jellyman552/idea-tracker.git/"})
    assert p.repo_url == "https://github.com/jellyman552/idea-tracker"
    for bad in ["github.com/a/b", "https://gitlab.com/a/b", "https://github.com/only-owner"]:
        with pytest.raises(ValidationError, match="github.com/owner/repo"):
            services.update_project(actor, p, {"repo_url": bad})
    services.update_project(actor, p, {"repo_url": ""})  # unlink
    assert p.repo_url == ""


def test_sync_stores_commits_and_dedupes(make_project):
    p = make_project(repo_url="https://github.com/o/r")
    fake = FakeGitHub({"o/r": [gh_commit("a" * 40, "First\n\nbody"), gh_commit("b" * 40)]})
    assert github_sync.sync_all(token="t", fetch=fake) == {p.title: 2}
    assert github_sync.sync_all(token="t", fetch=fake) == {p.title: 0}  # same commits again
    assert Commit.query.count() == 2
    first = Commit.query.filter_by(sha="a" * 40).one()
    assert first.headline == "First" and first.author == "Alex"
    assert "since=" in fake.calls[0]


def test_sync_survives_errors_and_keeps_going(make_project):
    broken = make_project(title="Broken", repo_url="https://github.com/o/gone")
    fine = make_project(title="Fine", repo_url="https://github.com/o/fine")
    fake = FakeGitHub({"o/gone": 404, "o/fine": [gh_commit("c" * 40)]})
    results = github_sync.sync_all(token="t", fetch=fake)
    assert "HTTP 404" in results["Broken"] and "token" in results["Broken"]
    assert results["Fine"] == 1
    assert Commit.query.count() == 1


def test_sync_skips_without_token_or_repo(make_project, monkeypatch):
    make_project(repo_url="https://github.com/o/r")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github_sync.sync_all() == {}


def test_paginates_until_short_page(make_project):
    make_project(repo_url="https://github.com/o/r")
    pages = [[gh_commit(f"{i:040d}") for i in range(100)], [gh_commit("f" * 40)]]

    def fetch(url, token):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return pages[page - 1]

    assert list(github_sync.sync_all(token="t", fetch=fetch).values()) == [101]


def test_commits_count_as_activity(make_project, actor):
    p = make_project(status="active", repo_url="https://github.com/o/r")
    then = utcnow() - timedelta(days=30)
    p.created_at = then
    for row in AuditLog.query.filter_by(project_id=p.id):
        row.ts = then
    db.session.commit()
    assert activity.staleness([p])[p.id]["stale"] is True
    github_sync.sync_all(token="t", fetch=FakeGitHub({"o/r": [gh_commit("d" * 40)]}))
    assert activity.staleness([p])[p.id]["stale"] is False
    ev = [e for e in activity.events(project_id=p.id) if e.type == "commit"]
    assert len(ev) == 1 and ev[0].source == "github"
    week = reviews.compute("weekly")
    assert week["totals"]["commits"] == 1
    assert week["commits_by_project"] == [{"id": p.id, "title": p.title, "commits": 1}]


def test_project_page_and_form(auth, make_project):
    p = make_project(repo_url="https://github.com/o/r")
    github_sync.sync_all(token="t", fetch=FakeGitHub({"o/r": [gh_commit("e" * 40, "Add reviews tab")]}))
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert "Recent commits" in html and "Add reviews tab" in html and "eeeeeee" in html
    assert 'name="repo_url"' in auth.get(f"/projects/{p.id}/edit").get_data(as_text=True)


def test_sync_cli(app, make_project, monkeypatch):
    make_project(title="CLI one", repo_url="https://github.com/o/r")
    monkeypatch.setattr(github_sync, "fetch_json", FakeGitHub({"o/r": [gh_commit("1" * 40)]}))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    result = app.test_cli_runner().invoke(args=["sync-github"])
    assert "CLI one: 1 new commit(s)" in result.output


def test_mcp_repo_link_and_commits(app, monkeypatch, make_project):
    monkeypatch.setattr(mcp_server, "app", app)
    p = make_project()
    mcp_server.update_project(p.id, repo_url="https://github.com/o/r")
    db.session.expire_all()  # the tool committed in its own session
    github_sync.sync_all(token="t", fetch=FakeGitHub({"o/r": [gh_commit("9" * 40, "Ship it")]}))
    detail = mcp_server.get_project(p.id)
    assert detail["repo_url"] == "https://github.com/o/r"
    assert detail["recent_commits"][0]["message"] == "Ship it"
