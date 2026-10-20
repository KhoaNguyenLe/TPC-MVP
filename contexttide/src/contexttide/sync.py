from __future__ import annotations

import re
import sqlite3
import uuid

from .providers import AccessRevoked, google_doc, local_files, slack_messages
from .store import finish_sync, mark_source_unavailable, now_iso, prune_items, upsert_item


SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
]


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED SECRET]", text)
    return text


def sync_project(db: sqlite3.Connection, project_id: str, days: int = 30) -> list[dict[str, object]]:
    sources = db.execute(
        "SELECT * FROM sources WHERE project_id=? AND enabled=1 ORDER BY id", (project_id,)
    ).fetchall()
    if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise ValueError(f"Unknown project: {project_id}")
    results: list[dict[str, object]] = []
    for source in sources:
        run_id = uuid.uuid4().hex
        count = 0
        try:
            if source["provider"] == "slack":
                records = slack_messages(source["external_id"], days=days)
            elif source["provider"] == "google_doc":
                records = [google_doc(source["external_id"])]
            else:
                records = local_files(source["external_id"])
            for record in records:
                if not record["body"].strip():
                    continue
                external_id = record["external_id"]
                item_id = f"{source['provider']}:{source['id']}:{external_id}"
                upsert_item(
                    db, item_id=item_id, project_id=project_id, source_id=source["id"],
                    external_id=external_id, title=record["title"],
                    body=redact(record["body"]), url=record["url"],
                    author=record["author"], modified_at=record["modified_at"],
                    revision=record["revision"], seen_run=run_id,
                )
                count += 1
            finish_sync(db, source["id"], run_id)
            db.commit()
            results.append({"source_id": source["id"], "status": "ok", "items": count})
        except AccessRevoked as exc:
            mark_source_unavailable(db, source["id"])
            db.commit()
            results.append({"source_id": source["id"], "status": "unavailable", "error": str(exc)})
        except Exception:
            db.rollback()
            raise
    prune_items(db, retention_days=days)
    return results

