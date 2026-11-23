"""Self-hosting pieces: ensure-user, the service worker route, and the ASGI
router that serves the web app and the MCP server from one process."""
import asyncio
import json

from tracker import asgi
from tracker.models import User


# ---------- ensure-user ----------

def test_ensure_user_creates_and_resets(app, monkeypatch):
    runner = app.test_cli_runner()
    monkeypatch.setenv("ADMIN_USERNAME", "alex")
    monkeypatch.setenv("ADMIN_PASSWORD", "first-password")
    assert "Saved user 'alex'" in runner.invoke(args=["ensure-user"]).output
    assert "up to date" in runner.invoke(args=["ensure-user"]).output
    monkeypatch.setenv("ADMIN_PASSWORD", "second-password")
    runner.invoke(args=["ensure-user"])
    assert User.query.filter_by(username="alex").one().check_password("second-password")


def test_ensure_user_skips_when_unset(app, monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert "skipping" in app.test_cli_runner().invoke(args=["ensure-user"]).output


def test_ensure_user_rejects_short_password(app, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "alex")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    assert app.test_cli_runner().invoke(args=["ensure-user"]).exit_code != 0


# ---------- installable app ----------

def test_service_worker_served_from_root(app):
    r = app.test_client().get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.content_type
    assert app.test_client().get("/static/manifest.webmanifest").status_code == 200


# ---------- ASGI router ----------

async def _call(router, path, method="GET"):
    sent = []
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
             "client": ("127.0.0.1", 1234), "root_path": "", "http_version": "1.1"}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await router(scope, receive, send)
    return sent


def _app(tag):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": tag.encode()})
    return inner


def test_router_sends_mcp_paths_to_mcp_and_the_rest_to_web():
    router = asgi.Router(_app("web"), _app("mcp"))
    body = lambda sent: sent[-1]["body"].decode()
    assert body(asyncio.run(_call(router, "/mcp/abc"))) == "mcp"
    assert body(asyncio.run(_call(router, "/"))) == "web"
    assert body(asyncio.run(_call(router, "/mcpx"))) == "web"


def test_router_without_mcp_serves_everything_from_web():
    router = asgi.Router(_app("web"))
    assert asyncio.run(_call(router, "/mcp/abc"))[-1]["body"] == b"web"


def test_router_lifespan_without_mcp():
    router = asgi.Router(_app("web"))
    msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent = []

    async def receive():
        return next(msgs)

    async def send(m):
        sent.append(m["type"])

    asyncio.run(router({"type": "lifespan"}, receive, send))
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_real_web_app_through_router(app):
    from a2wsgi import WSGIMiddleware

    router = asgi.Router(WSGIMiddleware(app))
    sent = asyncio.run(_call(router, "/login"))
    assert sent[0]["status"] == 200
