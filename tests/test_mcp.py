import json

import pytest

from tracker import mcp_server
from tracker.models import AuditLog, Project, db

TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def use_test_app(app, monkeypatch):
    monkeypatch.setattr(mcp_server, "app", app)


def test_create_project_with_chat_link_and_milestones():
    result = mcp_server.create_project(
        title="Sticker shop", type="business", summary="Chemistry puns",
        chat_link="https://claude.ai/chat/abc", status="active",
        milestones=["Design 5 stickers", "Open shop"])
    assert result["chat_link"] == "https://claude.ai/chat/abc"
    assert [m["title"] for m in result["milestones"]] == ["Design 5 stickers", "Open shop"]
    assert result["progress"] == {"done": 0, "total": 2, "percent": 0}
    rows = AuditLog.query.filter_by(project_id=result["id"]).all()
    assert rows and all((r.user, r.source) == ("claude", "mcp") for r in rows)


def test_create_project_validates():
    with pytest.raises(ValueError, match="Type must be one of"):
        mcp_server.create_project(title="X", type="hobby", summary="", chat_link="")
    with pytest.raises(ValueError, match="https://"):
        mcp_server.create_project(title="X", type="DIY", summary="", chat_link="javascript:alert(1)")
    assert Project.query.count() == 0


def test_list_and_get(make_project):
    make_project(title="Alpha", status="idea")
    beta = make_project(title="Beta", status="active", summary="herbs")
    assert {p["title"] for p in mcp_server.list_projects()} == {"Alpha", "Beta"}
    assert [p["title"] for p in mcp_server.list_projects(status="active")] == ["Beta"]
    assert [p["title"] for p in mcp_server.list_projects(query="HERB")] == ["Beta"]
    detail = mcp_server.get_project(beta.id)
    assert detail["summary"] == "herbs" and detail["milestones"] == []
    with pytest.raises(ValueError, match="No project with id 999"):
        mcp_server.get_project(999)


def test_update_project_and_notes(make_project):
    p = make_project(status="idea")
    result = mcp_server.update_project(p.id, status="active", summary="now real")
    assert result["changed"] == ["status", "summary"]
    note = mcp_server.add_note(p.id, "Kicked off")
    assert note["date"] == mcp_server._today().isoformat()
    mcp_server.update_note(note["id"], "Kicked off properly", "2026-10-01")
    assert mcp_server.get_project(p.id)["notes"][0]["note"] == "Kicked off properly"


def test_milestones_drive_status(make_project):
    p = make_project(status="active")
    ms = mcp_server.add_milestones(p.id, ["One", "Two"])["milestones"]
    assert mcp_server.set_milestone_done(ms[0]["id"])["status_changed_to"] is None
    assert mcp_server.set_milestone_done(ms[1]["id"])["status_changed_to"] == "done"
    assert mcp_server.set_milestone_done(ms[1]["id"], done=False)["status_changed_to"] == "active"


def test_archive_tools_hide_and_lock(make_project):
    p = make_project(title="Old idea")
    note = mcp_server.add_note(p.id, "a note")
    ms = mcp_server.add_milestones(p.id, ["m"])["milestones"][0]
    mcp_server.archive_note(note["id"])
    mcp_server.archive_milestone(ms["id"])
    mcp_server.archive_project(p.id)
    db.session.expire_all()  # the tool committed in its own session
    assert db.session.get(Project, p.id).archived_at is not None
    assert mcp_server.list_projects() == []
    with pytest.raises(ValueError, match="read-only"):
        mcp_server.add_note(p.id, "too late")


# ---------- Over HTTP, as the Claude apps call it ----------

@pytest.fixture
def http(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "idea-tracker.example")
    with TestClient(mcp_server.http_app(), base_url="https://idea-tracker.example") as client:
        yield client


def rpc(client, method, params=None, path=f"/mcp/{TOKEN}", headers=None):
    return client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                       headers={"Accept": "application/json, text/event-stream", **(headers or {})})


def initialize(client):
    return rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}})


def test_http_lists_and_calls_tools(http, make_project):
    assert initialize(http).status_code == 200
    names = {t["name"] for t in rpc(http, "tools/list").json()["result"]["tools"]}
    assert {"create_project", "add_note", "set_milestone_done", "archive_project"} <= names
    make_project(title="Over the wire")
    r = rpc(http, "tools/call", {"name": "list_projects", "arguments": {}})
    body = r.json()["result"]
    assert not body.get("isError")
    assert "Over the wire" in json.dumps(body)


def test_http_rejects_wrong_token(http):
    assert rpc(http, "tools/list", path="/mcp/" + "x" * 40).status_code == 404
    assert rpc(http, "tools/list", path="/mcp").status_code == 404


def test_http_rejects_unknown_host(http):
    r = rpc(http, "tools/list", headers={"Host": "evil.example"})
    assert r.status_code in (400, 421)


def test_http_app_requires_long_token(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "short")
    with pytest.raises(RuntimeError):
        mcp_server.http_app()
