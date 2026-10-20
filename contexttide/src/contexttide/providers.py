from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import load_settings


class ProviderError(RuntimeError):
    pass


class AccessRevoked(ProviderError):
    pass


LOCAL_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".jsx", ".ts", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".html", ".css", ".sql", ".go", ".java",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".contexttide"}


def local_files(folder_path: str, max_files: int = 500) -> Iterator[dict[str, str]]:
    """Read explicitly selected text files from one folder, without following symlinks."""
    root = Path(folder_path)
    if not root.is_dir():
        raise AccessRevoked(f"Local folder is unavailable: {root}")
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink() or path.suffix.lower() not in LOCAL_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 250_000:
                    continue
                body = path.read_text(encoding="utf-8", errors="replace")
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            except (OSError, UnicodeError):
                continue
            relative = path.relative_to(root).as_posix()
            yield {
                "external_id": relative,
                "title": relative,
                "body": body,
                "url": path.resolve().as_uri(),
                "author": "",
                "modified_at": modified,
                "revision": modified,
            }
            count += 1
            if count >= max_files:
                return


def _request_json(url: str, token: str, *, form: dict[str, str] | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    for attempt in range(4):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                **({"Content-Type": "application/x-www-form-urlencoded"} if body else {}),
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise AccessRevoked(f"Provider access failed ({exc.code})") from exc
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt < 3:
                    time.sleep(min(int(exc.headers.get("Retry-After", "0") or "0") or 2 ** attempt, 30))
                    continue
            raise ProviderError(f"Provider request failed ({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise ProviderError("Provider request failed after retries") from exc
    raise ProviderError("Provider request failed after retries")


def _slack_call(method: str, token: str, **params: str | int) -> dict[str, Any]:
    url = "https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params)
    data = _request_json(url, token)
    if not data.get("ok"):
        code = str(data.get("error", "unknown_error"))
        if code in {"not_in_channel", "channel_not_found", "missing_scope", "invalid_auth", "token_revoked"}:
            raise AccessRevoked(f"Slack access failed: {code}")
        raise ProviderError(f"Slack API error: {code}")
    return data


def _slack_page(method: str, token: str, **params: str | int) -> Iterator[dict[str, Any]]:
    cursor = ""
    while True:
        response = _slack_call(method, token, **params, **({"cursor": cursor} if cursor else {}))
        for message in response.get("messages", []):
            yield message
        cursor = response.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break


def slack_messages(channel_id: str, days: int = 30) -> Iterator[dict[str, str]]:
    load_settings()
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        raise ProviderError("SLACK_BOT_TOKEN is not configured")
    oldest = str(int(time.time()) - days * 86400)
    for message in _slack_page("conversations.history", token, channel=channel_id, oldest=oldest, limit=200):
        yield _slack_record(channel_id, message)
        if message.get("reply_count", 0):
            for reply in _slack_page(
                "conversations.replies", token, channel=channel_id,
                ts=str(message["ts"]), oldest=oldest, limit=200,
            ):
                if reply.get("ts") != message.get("ts"):
                    yield _slack_record(channel_id, reply)


def _slack_record(channel_id: str, message: dict[str, Any]) -> dict[str, str]:
    ts = str(message["ts"])
    modified = message.get("edited", {}).get("ts", ts)
    return {
        "external_id": ts,
        "title": f"Slack #{channel_id}",
        "body": str(message.get("text", "")),
        "url": f"https://app.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
        "author": str(message.get("user", message.get("bot_id", ""))),
        "modified_at": datetime.fromtimestamp(float(modified), timezone.utc).isoformat(timespec="seconds"),
        "revision": str(modified),
    }


def google_access_token() -> str:
    load_settings()
    token = os.getenv("GOOGLE_ACCESS_TOKEN", "")
    if token:
        return token
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if not all((client_id, client_secret, refresh_token)):
        raise ProviderError("Configure GOOGLE_ACCESS_TOKEN or Google client ID, secret, and refresh token")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return str(json.load(response)["access_token"])
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as exc:
        raise ProviderError("Could not refresh Google access token") from exc


def _elements_text(elements: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for element in elements:
        if "paragraph" in element:
            for child in element["paragraph"].get("elements", []):
                parts.append(child.get("textRun", {}).get("content", ""))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    parts.append(_elements_text(cell.get("content", [])))
        elif "tableOfContents" in element:
            parts.append(_elements_text(element["tableOfContents"].get("content", [])))
    return "".join(parts)


def _tabs_text(tabs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "")
        if title:
            parts.append(f"\n## {title}\n")
        parts.append(_elements_text(tab.get("documentTab", {}).get("body", {}).get("content", [])))
        parts.append(_tabs_text(tab.get("childTabs", [])))
    return "".join(parts)


def google_doc(document_id: str) -> dict[str, str]:
    token = google_access_token()
    url = "https://docs.googleapis.com/v1/documents/" + urllib.parse.quote(document_id, safe="") + "?includeTabsContent=true"
    data = _request_json(url, token)
    content = _tabs_text(data.get("tabs", [])) if data.get("tabs") else _elements_text(data.get("body", {}).get("content", []))
    return {
        "external_id": document_id,
        "title": str(data.get("title", "Google Doc")),
        "body": content[:200_000],
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
        "author": "",
        "modified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": str(data.get("revisionId", "")),
    }

