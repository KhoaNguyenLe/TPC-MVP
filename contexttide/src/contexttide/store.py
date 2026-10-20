from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import db_path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      repo_url TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sources (
      id INTEGER PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      provider TEXT NOT NULL CHECK(provider IN ('slack','google_doc','local_folder')),
      external_id TEXT NOT NULL,
      name TEXT NOT NULL DEFAULT '',
      enabled INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL DEFAULT 'ready',
      last_synced_at TEXT,
      UNIQUE(project_id, provider, external_id)
    );
    CREATE TABLE IF NOT EXISTS items (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      external_id TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',
      body TEXT NOT NULL,
      url TEXT NOT NULL DEFAULT '',
      author TEXT NOT NULL DEFAULT '',
      modified_at TEXT NOT NULL,
      revision TEXT NOT NULL DEFAULT '',
      deleted INTEGER NOT NULL DEFAULT 0,
      seen_run TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS items_project ON items(project_id, deleted, modified_at);
    CREATE INDEX IF NOT EXISTS items_source ON items(source_id, seen_run);
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_id UNINDEXED, title, body);
    CREATE TABLE IF NOT EXISTS facts (
      id INTEGER PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
      kind TEXT NOT NULL CHECK(kind IN ('rule','decision','active_work','blocker','open_question')),
      text TEXT NOT NULL,
      source_url TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'approved',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS facts_project ON facts(project_id, status, kind);
    CREATE TABLE IF NOT EXISTS context_requests (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      tool TEXT NOT NULL,
      task TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS context_requests_project ON context_requests(project_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS context_request_links (
      id INTEGER PRIMARY KEY,
      request_id TEXT NOT NULL REFERENCES context_requests(id) ON DELETE CASCADE,
      item_id TEXT NOT NULL,
      provider TEXT NOT NULL,
      title TEXT NOT NULL,
      url TEXT NOT NULL,
      action TEXT NOT NULL CHECK(action IN ('returned','expanded')),
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS context_request_links_request ON context_request_links(request_id);
    """)
    source_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'").fetchone()[0]
    if "local_folder" not in source_schema:
        db.executescript("""
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE sources_new (
          id INTEGER PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          provider TEXT NOT NULL CHECK(provider IN ('slack','google_doc','local_folder')),
          external_id TEXT NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'ready',
          last_synced_at TEXT,
          UNIQUE(project_id, provider, external_id)
        );
        INSERT INTO sources_new SELECT id,project_id,provider,external_id,name,enabled,status,last_synced_at FROM sources;
        DROP TABLE sources;
        ALTER TABLE sources_new RENAME TO sources;
        COMMIT;
        PRAGMA foreign_keys=ON;
        """)


def add_project(db: sqlite3.Connection, project_id: str, name: str, summary: str = "", repo_url: str = "") -> None:
    db.execute(
        "INSERT INTO projects(id,name,summary,repo_url,created_at) VALUES(?,?,?,?,?)",
        (project_id, name, summary, repo_url, now_iso()),
    )


def add_source(db: sqlite3.Connection, project_id: str, provider: str, external_id: str, name: str = "") -> int:
    row = db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown project: {project_id}")
    if provider == "local_folder":
        folder = Path(external_id).expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {external_id}")
        external_id = str(folder)
    cur = db.execute(
        "INSERT INTO sources(project_id,provider,external_id,name) VALUES(?,?,?,?)",
        (project_id, provider, external_id, name),
    )
    return int(cur.lastrowid)


def add_fact(db: sqlite3.Connection, project_id: str, kind: str, text: str, source_id: int | None = None, source_url: str = "") -> int:
    if kind not in {"rule", "decision", "active_work", "blocker", "open_question"}:
        raise ValueError("Invalid fact kind")
    if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise ValueError(f"Unknown project: {project_id}")
    if source_id is not None and db.execute(
        "SELECT 1 FROM sources WHERE id=? AND project_id=?", (source_id, project_id)
    ).fetchone() is None:
        raise ValueError("Source does not belong to project")
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO facts(project_id,source_id,kind,text,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, source_id, kind, text.strip(), source_url, ts, ts),
    )
    return int(cur.lastrowid)


def upsert_item(
    db: sqlite3.Connection, *, item_id: str, project_id: str, source_id: int,
    external_id: str, title: str, body: str, url: str, author: str,
    modified_at: str, revision: str, seen_run: str,
) -> None:
    db.execute("""
      INSERT INTO items(id,project_id,source_id,external_id,title,body,url,author,modified_at,revision,deleted,seen_run)
      VALUES(?,?,?,?,?,?,?,?,?,?,0,?)
      ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body,
        url=excluded.url, author=excluded.author, modified_at=excluded.modified_at,
        revision=excluded.revision, deleted=0, seen_run=excluded.seen_run
    """, (item_id, project_id, source_id, external_id, title, body, url, author, modified_at, revision, seen_run))
    db.execute("DELETE FROM items_fts WHERE item_id=?", (item_id,))
    db.execute("INSERT INTO items_fts(item_id,title,body) VALUES(?,?,?)", (item_id, title, body))


def mark_source_unavailable(db: sqlite3.Connection, source_id: int) -> None:
    db.execute("UPDATE sources SET status='unavailable',last_synced_at=? WHERE id=?", (now_iso(), source_id))
    purge_source_items(db, source_id)
    db.execute("UPDATE facts SET status='stale',updated_at=? WHERE source_id=?", (now_iso(), source_id))


def purge_source_items(db: sqlite3.Connection, source_id: int) -> None:
    db.execute("DELETE FROM items_fts WHERE item_id IN (SELECT id FROM items WHERE source_id=?)", (source_id,))
    db.execute("DELETE FROM items WHERE source_id=?", (source_id,))


def finish_sync(db: sqlite3.Connection, source_id: int, seen_run: str) -> None:
    db.execute("UPDATE items SET deleted=1 WHERE source_id=? AND seen_run<>?", (source_id, seen_run))
    db.execute("UPDATE sources SET status='ready',last_synced_at=? WHERE id=?", (now_iso(), source_id))


def prune_items(db: sqlite3.Connection, retention_days: int = 30) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    db.execute("DELETE FROM items_fts WHERE item_id IN (SELECT id FROM items WHERE deleted=1 OR modified_at < ?)", (cutoff,))
    db.execute("DELETE FROM items WHERE deleted=1 OR modified_at < ?", (cutoff,))


def search_items(db: sqlite3.Connection, project_id: str, task: str, limit: int = 8) -> list[sqlite3.Row]:
    terms = list(dict.fromkeys(re.findall(r"[\w-]{3,}", task.lower())))[:12]
    if not terms:
        return []
    query = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
    return db.execute("""
      SELECT i.*, bm25(items_fts) AS rank FROM items_fts
      JOIN items i ON i.id=items_fts.item_id
      JOIN sources s ON s.id=i.source_id
      WHERE items_fts MATCH ? AND i.project_id=? AND i.deleted=0 AND s.enabled=1 AND s.status='ready'
      ORDER BY rank ASC, i.modified_at DESC LIMIT ?
    """, (query, project_id, limit)).fetchall()

