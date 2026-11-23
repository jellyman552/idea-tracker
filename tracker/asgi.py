"""One process for everything: the Flask web app plus, when MCP_TOKEN is set,
the MCP server under /mcp/<MCP_TOKEN>. This is what the Docker image runs, so
self-hosting is one container on one port.

    uvicorn --factory tracker.asgi:create --port 8000 --no-access-log

(--no-access-log matters: the MCP path contains the secret token.)
"""

import os

from a2wsgi import WSGIMiddleware

from . import create_app


async def _lifespan_ok(receive, send):
    """Minimal ASGI lifespan for when there is no MCP app to delegate it to."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


class Router:
    """Sends /mcp/* to the MCP app and everything else to Flask."""

    def __init__(self, web, mcp=None):
        self.web = web
        self.mcp = mcp

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            if self.mcp:  # the MCP session manager needs its startup/shutdown hooks
                return await self.mcp(scope, receive, send)
            return await _lifespan_ok(receive, send)
        if self.mcp and scope.get("path", "").startswith("/mcp/"):
            return await self.mcp(scope, receive, send)
        return await self.web(scope, receive, send)


def create():
    web = WSGIMiddleware(create_app())
    mcp = None
    if os.environ.get("MCP_TOKEN"):
        from .mcp_server import http_app

        mcp = http_app()
    return Router(web, mcp)
