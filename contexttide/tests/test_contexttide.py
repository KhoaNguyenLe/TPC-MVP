from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contexttide.context import project_context, source_excerpt  # noqa: E402
from contexttide.audit import recent_requests, write_report  # noqa: E402
from contexttide.providers import _tabs_text  # noqa: E402
from contexttide.server import mcp  # noqa: E402
from contexttide.store import add_fact, add_project, add_source, connect, init_db, upsert_item  # noqa: E402
from contexttide.sync import redact, sync_project  # noqa: E402


class ContextTideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.sqlite3"
        self.home_env = patch.dict(os.environ, {"CONTEXTTIDE_HOME": self.temp.name, "CONTEXTTIDE_DB": str(self.path)})
        self.home_env.start()
        self.addCleanup(self.home_env.stop)
        with connect(self.path) as db:
            init_db(db)
            add_project(db, "alpha", "Alpha", "An internal API")
            add_project(db, "beta", "Beta", "A separate project")

    def test_retrieval_and_source_boundary(self) -> None:
        with connect(self.path) as db:
            source_id = add_source(db, "alpha", "slack", "C123", "engineering")
            add_fact(db, "alpha", "rule", "Use strict TypeScript.")
            upsert_item(
                db, item_id="slack:1:123.45", project_id="alpha", source_id=source_id,
                external_id="123.45", title="Slack engineering", body="Login errors use a banner.",
                url="https://example.test/message", author="alice",
                modified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                revision="123.45", seen_run="one",
            )
            result = project_context(db, "alpha", "login error handling")
            self.assertEqual(result["approved_facts"][0]["text"], "Use strict TypeScript.")
            self.assertEqual(result["relevant_sources"][0]["id"], "slack:1:123.45")
            with self.assertRaises(ValueError):
                source_excerpt(db, "beta", "slack:1:123.45")
            db.execute("UPDATE sources SET enabled=0 WHERE id=?", (source_id,))
            self.assertEqual(project_context(db, "alpha", "login error")["relevant_sources"], [])

    def test_sync_redacts_and_reconciles(self) -> None:
        with connect(self.path) as db:
            source_id = add_source(db, "alpha", "slack", "C123")
            record = {
                "external_id": "123.45", "title": "Slack engineering",
                "body": "API key sk-abcdefghijklmnopqrstuvwxyz12345 for login flow",
                "url": "https://example.test", "author": "alice",
                "modified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "revision": "123.45",
            }
            with patch("contexttide.sync.slack_messages", return_value=iter([record])):
                self.assertEqual(sync_project(db, "alpha")[0]["items"], 1)
            self.assertIn("[REDACTED SECRET]", db.execute("SELECT body FROM items").fetchone()[0])
            with patch("contexttide.sync.slack_messages", return_value=iter([])):
                sync_project(db, "alpha")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE source_id=?", (source_id,)).fetchone()[0], 0)

    def test_google_tabs_and_mcp_call(self) -> None:
        tabs = [{
            "tabProperties": {"title": "Main"},
            "documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "First tab"}}]}}]}},
            "childTabs": [{
                "tabProperties": {"title": "Child"},
                "documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Second tab"}}]}}]}},
            }],
        }]
        self.assertIn("First tab", _tabs_text(tabs))
        self.assertIn("Second tab", _tabs_text(tabs))

        async def call() -> None:
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_project_context", {"project_id": "alpha", "task": "login"})
                self.assertEqual(result.structured_content["project"]["id"], "alpha")

        asyncio.run(call())

    def test_mcp_activity_report_tracks_returned_and_expanded_links(self) -> None:
        with connect(self.path) as db:
            source_id = add_source(db, "alpha", "google_doc", "doc123")
            add_fact(db, "alpha", "decision", "Use the roadmap", source_url="https://example.test/decision")
            upsert_item(
                db, item_id=f"google_doc:{source_id}:doc123", project_id="alpha", source_id=source_id,
                external_id="doc123", title="Roadmap", body="Milestone violet is next.",
                url="https://docs.google.com/document/d/doc123/edit", author="",
                modified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                revision="1", seen_run="one",
            )

        async def call() -> None:
            from fastmcp import Client
            async with Client(mcp) as client:
                result = await client.call_tool("get_project_context", {"project_id": "alpha", "task": "violet milestone"})
                data = result.structured_content
                self.assertEqual(len(data["relevant_sources"]), 1)
                self.assertTrue(data["request_id"])
                await client.call_tool("get_source_excerpt", {
                    "project_id": "alpha", "source_item_id": data["relevant_sources"][0]["id"],
                    "request_id": data["request_id"],
                })

        asyncio.run(call())
        with connect(self.path) as db:
            entries = recent_requests(db, "alpha")
            self.assertEqual(len(entries), 1)
            self.assertEqual([link["action"] for link in entries[0]["links"]], ["returned", "returned", "expanded"])
            report = Path(self.temp.name) / "report.html"
            write_report(db, "alpha", report)
            page = report.read_text(encoding="utf-8")
            self.assertIn("Roadmap", page)
            self.assertIn("https://docs.google.com/document/d/doc123/edit", page)
            self.assertNotIn("Milestone violet is next.", page)

    def test_http_guard_rejects_missing_token(self) -> None:
        from contexttide.server import BearerGuard

        reached = []
        sent = []

        async def app(scope, receive, send):
            reached.append(True)

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        guard = BearerGuard(app, "x" * 32)
        asyncio.run(guard({"type": "http", "headers": []}, receive, send))
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(reached, [])
        asyncio.run(guard({"type": "http", "headers": [(b"authorization", b"Bearer " + b"x" * 32)]}, receive, send))
        self.assertEqual(reached, [True])

    def test_stdio_from_another_working_directory(self) -> None:
        async def call() -> None:
            from fastmcp import Client
            from fastmcp.client.transports import StdioTransport

            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            transport = StdioTransport(sys.executable, ["-m", "contexttide", "serve"], env=env, cwd=self.temp.name)
            async with Client(transport) as client:
                result = await client.call_tool("list_team_projects")
                self.assertEqual(len(result.structured_content["result"]), 2)

        asyncio.run(call())

    def test_local_folder_sync_and_removal(self) -> None:
        folder = Path(self.temp.name) / "sample-project"
        folder.mkdir()
        (folder / "notes.md").write_text("Login errors appear in a banner.", encoding="utf-8")
        (folder / ".env").write_text("SECRET=do-not-index", encoding="utf-8")
        with connect(self.path) as db:
            source_id = add_source(db, "alpha", "local_folder", str(folder))
            self.assertEqual(sync_project(db, "alpha")[0]["items"], 1)
            result = project_context(db, "alpha", "login errors")
            self.assertIn("banner", result["relevant_sources"][0]["excerpt"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items WHERE source_id=?", (source_id,)).fetchone()[0], 1)
            (folder / "notes.md").unlink()
            sync_project(db, "alpha")
            self.assertEqual(project_context(db, "alpha", "login errors")["relevant_sources"], [])

    def test_existing_database_migrates_for_local_folder(self) -> None:
        old_path = Path(self.temp.name) / "old.sqlite3"
        old = sqlite3.connect(old_path)
        old.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY, project_id TEXT, provider TEXT CHECK(provider IN ('slack','google_doc')), external_id TEXT, name TEXT DEFAULT '', enabled INTEGER DEFAULT 1, status TEXT DEFAULT 'ready', last_synced_at TEXT)")
        old.commit()
        old.close()
        with connect(old_path) as db:
            init_db(db)
            add_project(db, "old", "Old project")
            folder = Path(self.temp.name) / "folder"
            folder.mkdir()
            add_source(db, "old", "local_folder", str(folder))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()

