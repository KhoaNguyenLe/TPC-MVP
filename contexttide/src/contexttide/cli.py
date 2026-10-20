from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .audit import recent_requests, write_report
from .config import db_path
from .context import build_prompt, list_projects
from .server import serve
from .store import add_fact, add_project, add_source, connect, init_db, now_iso, purge_source_items
from .sync import sync_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contexttide", description="Team context MCP service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create an empty local context database")

    project = sub.add_parser("project", help="Manage projects")
    project_sub = project.add_subparsers(dest="action", required=True)
    project_sub.add_parser("list")
    add = project_sub.add_parser("add")
    add.add_argument("id", help="Stable ID, e.g. my-project")
    add.add_argument("--name", required=True)
    add.add_argument("--summary", default="")
    add.add_argument("--repo-url", default="")
    update = project_sub.add_parser("update")
    update.add_argument("id")
    update.add_argument("--name")
    update.add_argument("--summary")
    update.add_argument("--repo-url")

    source = sub.add_parser("source", help="Manage selected provider sources")
    source_sub = source.add_subparsers(dest="action", required=True)
    source_add = source_sub.add_parser("add")
    source_add.add_argument("project_id")
    source_add.add_argument("provider", choices=["slack", "google_doc", "local_folder"])
    source_add.add_argument("external_id", help="Slack channel ID, Google document ID, or local folder path")
    source_add.add_argument("--name", default="")
    source_list = source_sub.add_parser("list")
    source_list.add_argument("project_id")
    source_disable = source_sub.add_parser("disable")
    source_disable.add_argument("source_id", type=int)
    source_enable = source_sub.add_parser("enable")
    source_enable.add_argument("source_id", type=int)

    fact = sub.add_parser("fact", help="Manage approved project facts")
    fact_sub = fact.add_subparsers(dest="action", required=True)
    fact_add = fact_sub.add_parser("add")
    fact_add.add_argument("project_id")
    fact_add.add_argument("kind", choices=["rule", "decision", "active_work", "blocker", "open_question"])
    fact_add.add_argument("text")
    fact_add.add_argument("--source-id", type=int)
    fact_add.add_argument("--source-url", default="")
    fact_list = fact_sub.add_parser("list")
    fact_list.add_argument("project_id")
    fact_stale = fact_sub.add_parser("stale")
    fact_stale.add_argument("fact_id", type=int)

    sync = sub.add_parser("sync", help="Read selected Slack channels and Google Docs")
    sync.add_argument("project_id")
    sync.add_argument("--days", type=int, default=30)

    prompt = sub.add_parser("prompt", help="Preview a task prompt")
    prompt.add_argument("project_id")
    prompt.add_argument("task")

    audit = sub.add_parser("audit", help="Inspect MCP context requests and source links")
    audit_sub = audit.add_subparsers(dest="action", required=True)
    audit_list = audit_sub.add_parser("list", help="Show recent requests as JSON")
    audit_list.add_argument("project_id")
    audit_report = audit_sub.add_parser("report", help="Generate a local HTML activity report")
    audit_report.add_argument("project_id")
    audit_report.add_argument("--output", type=Path)

    server = sub.add_parser("serve", help="Run the MCP server")
    server.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "serve":
        serve(args.transport, args.host, args.port)
        return
    with connect() as db:
        init_db(db)
        if args.command == "init":
            print("Initialized empty ContextTide deployment")
        elif args.command == "project":
            if args.action == "add":
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", args.id):
                    raise ValueError("Project ID must be 2-64 lowercase letters, digits, _ or -")
                add_project(db, args.id, args.name, args.summary, args.repo_url)
                print(f"Added project {args.id}")
            elif args.action == "update":
                changes = {"name": args.name, "summary": args.summary, "repo_url": args.repo_url}
                changes = {key: value for key, value in changes.items() if value is not None}
                if not changes:
                    raise ValueError("Provide at least one field to update")
                assignments = ", ".join(f"{key}=?" for key in changes)
                cur = db.execute(f"UPDATE projects SET {assignments} WHERE id=?", (*changes.values(), args.id))
                if not cur.rowcount:
                    raise ValueError(f"Unknown project: {args.id}")
                print(f"Updated project {args.id}")
            else:
                print(json.dumps(list_projects(db), indent=2))
        elif args.command == "source":
            if args.action == "add":
                source_id = add_source(db, args.project_id, args.provider, args.external_id, args.name)
                print(f"Added source {source_id}")
            elif args.action == "list":
                rows = db.execute(
                    "SELECT id,provider,external_id,name,enabled,status,last_synced_at FROM sources WHERE project_id=? ORDER BY id",
                    (args.project_id,),
                ).fetchall()
                print(json.dumps([dict(row) for row in rows], indent=2))
            elif args.action == "disable":
                db.execute("UPDATE sources SET enabled=0,status='disabled' WHERE id=?", (args.source_id,))
                purge_source_items(db, args.source_id)
                db.execute("UPDATE facts SET status='stale',updated_at=? WHERE source_id=?", (now_iso(), args.source_id))
                print(f"Disabled source {args.source_id}")
            else:
                db.execute("UPDATE sources SET enabled=1,status='ready',last_synced_at=NULL WHERE id=?", (args.source_id,))
                print(f"Enabled source {args.source_id}; run sync to refresh it")
        elif args.command == "fact":
            if args.action == "add":
                fact_id = add_fact(db, args.project_id, args.kind, args.text, args.source_id, args.source_url)
                print(f"Added approved fact {fact_id}")
            elif args.action == "list":
                rows = db.execute(
                    "SELECT id,kind,text,status,source_id,source_url,updated_at FROM facts WHERE project_id=? ORDER BY id",
                    (args.project_id,),
                ).fetchall()
                print(json.dumps([dict(row) for row in rows], indent=2))
            else:
                db.execute("UPDATE facts SET status='stale',updated_at=? WHERE id=?", (now_iso(), args.fact_id))
                print(f"Marked fact {args.fact_id} stale")
        elif args.command == "sync":
            if not 1 <= args.days <= 90:
                raise ValueError("--days must be between 1 and 90")
            print(json.dumps(sync_project(db, args.project_id, args.days), indent=2))
        elif args.command == "prompt":
            print(build_prompt(db, args.project_id, args.task))
        elif args.command == "audit":
            if args.action == "list":
                print(json.dumps(recent_requests(db, args.project_id), indent=2))
            else:
                output = args.output or db_path().parent / "reports" / f"{args.project_id}.html"
                print(write_report(db, args.project_id, output).resolve())


if __name__ == "__main__":
    main()

