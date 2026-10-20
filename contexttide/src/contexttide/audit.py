from __future__ import annotations

import html
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .store import now_iso


def record_context(db: sqlite3.Connection, project_id: str, tool: str, task: str, result: dict) -> str:
    """Store link metadata for one context response, without storing excerpts."""
    request_id = uuid.uuid4().hex
    created_at = now_iso()
    db.execute(
        "INSERT INTO context_requests(id,project_id,tool,task,created_at) VALUES(?,?,?,?,?)",
        (request_id, project_id, tool, task, created_at),
    )
    for source in result.get("relevant_sources", []):
        _record_link(db, request_id, source, "returned", created_at)
    for fact in result.get("approved_facts", []):
        if fact.get("source_url"):
            _record_link(db, request_id, {
                "id": f"approved_fact:{fact['id']}",
                "title": f"Approved {fact['kind']} source",
                "url": fact["source_url"],
            }, "returned", created_at)
    project = result.get("project", {})
    if project.get("repo_url"):
        _record_link(db, request_id, {
            "id": f"project_repo:{project_id}",
            "title": "Project repository",
            "url": project["repo_url"],
        }, "returned", created_at)
    return request_id


def record_excerpt(db: sqlite3.Connection, project_id: str, source: dict, request_id: str = "") -> str:
    """Associate an excerpt read with a context request when its ID is supplied."""
    linked = db.execute(
        "SELECT id FROM context_requests WHERE id=? AND project_id=?", (request_id, project_id)
    ).fetchone() if request_id else None
    if linked is None:
        request_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO context_requests(id,project_id,tool,task,created_at) VALUES(?,?,?,?,?)",
            (request_id, project_id, "get_source_excerpt", "", now_iso()),
        )
    _record_link(db, request_id, source, "expanded", now_iso())
    return request_id


def _record_link(db: sqlite3.Connection, request_id: str, source: dict, action: str, created_at: str) -> None:
    item_id = str(source["id"])
    db.execute(
        "INSERT INTO context_request_links(request_id,item_id,provider,title,url,action,created_at) VALUES(?,?,?,?,?,?,?)",
        (request_id, item_id, item_id.split(":", 1)[0], str(source["title"]), str(source["url"]), action, created_at),
    )


def recent_requests(db: sqlite3.Connection, project_id: str, limit: int = 100) -> list[dict]:
    if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise ValueError(f"Unknown project: {project_id}")
    rows = db.execute(
        "SELECT id,tool,task,created_at FROM context_requests WHERE project_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
        (project_id, max(1, min(limit, 500))),
    ).fetchall()
    result = []
    for row in rows:
        links = db.execute(
            "SELECT item_id,provider,title,url,action,created_at FROM context_request_links WHERE request_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
        result.append({**dict(row), "links": [dict(link) for link in links]})
    return result


def _safe_url(url: str) -> str:
    if urlsplit(url).scheme not in {"https", "http", "file"}:
        return "#"
    return html.escape(url, quote=True)


def write_report(db: sqlite3.Connection, project_id: str, output: Path, limit: int = 100) -> Path:
    requests = recent_requests(db, project_id, limit)
    cards = []
    for request in requests:
        links = request["links"]
        returned = sum(link["action"] == "returned" for link in links)
        expanded = sum(link["action"] == "expanded" for link in links)
        link_rows = "".join(
            f'<li><span class="badge">{html.escape(link["provider"])}</span> '
            f'<a href="{_safe_url(link["url"])}">{html.escape(link["title"])}</a> '
            f'<span class="muted">{html.escape(link["action"])}</span></li>'
            for link in links
        ) or '<li class="muted">No source links returned</li>'
        cards.append(
            '<article class="card">'
            f'<div class="meta"><time>{html.escape(request["created_at"])}</time>'
            f'<span>{html.escape(request["tool"])}</span></div>'
            f'<h2>{html.escape(request["task"] or "Source excerpt")}</h2>'
            f'<p>{returned} links returned | {expanded} excerpts expanded</p>'
            f'<details><summary>View source links</summary><ul>{link_rows}</ul></details>'
            f'<small>Request ID: {html.escape(request["id"])}</small>'
            '</article>'
        )
    content = "\n".join(cards) or '<p class="empty">No MCP context requests recorded yet. Ask Codex to call get_project_context, then regenerate this report.</p>'
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ContextTide activity | {html.escape(project_id)}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;background:#f4f7f8;color:#14242b;margin:0}}
main{{max-width:880px;margin:3rem auto;padding:0 1rem}}
h1{{margin-bottom:.25rem}} .muted,small,time{{color:#60727b}}
.flow{{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:1.5rem 0;padding:1rem;background:#e7f5f1;border-radius:12px}}
.flow strong{{background:white;padding:.5rem .8rem;border-radius:8px}}
.card{{background:white;border:1px solid #d8e2e5;border-radius:12px;padding:1.2rem;margin:1rem 0;box-shadow:0 2px 6px #14242b0a}}
.card h2{{font-size:1.15rem;margin:.6rem 0;overflow-wrap:anywhere}} .card p{{margin:.4rem 0 1rem}}
.meta{{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;font-size:.85rem}}
details{{border-top:1px solid #e3ebee;padding-top:.75rem;margin-bottom:.75rem}}
summary{{cursor:pointer;font-weight:600}} ul{{padding-left:1.2rem}} li{{margin:.55rem 0;overflow-wrap:anywhere}}
.badge{{background:#dceee9;color:#16513e;padding:.2rem .45rem;border-radius:5px;font-size:.8rem}}
a{{color:#075e8c}} .empty{{padding:2rem;background:white;border-radius:12px}}
</style></head><body><main>
<h1>ContextTide activity</h1><p class="muted">Project: {html.escape(project_id)} | {len(requests)} recent requests</p>
<div class="flow"><strong>Codex task</strong> → <strong>ContextTide retrieval</strong> → <strong>Source links returned</strong> → <strong>Optional excerpt reads</strong></div>
<p class="muted">This report shows what ContextTide sent through MCP. It cannot see links Codex opens directly or prove which links influenced its final answer. Tasks and link metadata are stored locally; excerpts are not stored in this log.</p>
{content}
</main></body></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output
