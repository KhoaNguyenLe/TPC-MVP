from __future__ import annotations

import hmac
import os
from typing import Any

from fastmcp import FastMCP

from .audit import record_context, record_excerpt
from .config import load_settings
from .context import build_prompt, list_projects, project_context, source_excerpt
from .store import connect, init_db


mcp = FastMCP("ContextTide")


@mcp.tool()
def list_team_projects() -> list[dict[str, str]]:
    """List project IDs available in this team deployment."""
    with connect() as db:
        init_db(db)
        return list_projects(db)


@mcp.tool()
def get_project_context(project_id: str, task: str, max_sources: int = 8) -> dict[str, Any]:
    """Get approved project rules and task-relevant, cited work context. Call at task start and when the task changes."""
    with connect() as db:
        init_db(db)
        result = project_context(db, project_id, task, max_sources)
        result["request_id"] = record_context(db, project_id, "get_project_context", task, result)
        return result


@mcp.tool()
def get_source_excerpt(project_id: str, source_item_id: str, request_id: str = "") -> dict[str, str]:
    """Read more of a returned source. Pass the request_id from get_project_context to link the read in the activity report."""
    with connect() as db:
        init_db(db)
        result = source_excerpt(db, project_id, source_item_id)
        result["request_id"] = record_excerpt(db, project_id, result, request_id)
        return result


@mcp.tool()
def make_task_prompt(project_id: str, task: str) -> str:
    """Build a copyable task prompt with approved facts and cited source evidence."""
    with connect() as db:
        init_db(db)
        context = project_context(db, project_id, task)
        record_context(db, project_id, "make_task_prompt", task, context)
        return build_prompt(db, project_id, task, context)


class BearerGuard:
    """Minimal team-token guard for self-hosted HTTP mode."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = dict(scope.get("headers", []))
        value = headers.get(b"authorization", b"")
        expected = b"Bearer " + self.token
        if not hmac.compare_digest(value, expected):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.app(scope, receive, send)


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    load_settings()
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        token = os.getenv("CONTEXTTIDE_HTTP_TOKEN", "")
        if len(token) < 32:
            raise ValueError("HTTP mode requires CONTEXTTIDE_HTTP_TOKEN with at least 32 characters")
        if host not in {"127.0.0.1", "localhost", "::1"} and os.getenv("CONTEXTTIDE_ALLOW_REMOTE_HTTP") != "1":
            raise ValueError("Remote HTTP binding requires CONTEXTTIDE_ALLOW_REMOTE_HTTP=1 and an HTTPS reverse proxy")
        import uvicorn

        app = BearerGuard(mcp.http_app(path="/mcp"), token)
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        raise ValueError("Transport must be stdio or http")

