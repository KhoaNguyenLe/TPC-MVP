from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def project_root() -> Path:
    if os.getenv("CONTEXTTIDE_HOME"):
        return Path(os.environ["CONTEXTTIDE_HOME"]).resolve()
    return Path(__file__).resolve().parents[2]


def load_settings() -> None:
    load_dotenv(project_root() / ".env", override=False)


def db_path() -> Path:
    load_settings()
    raw = Path(os.getenv("CONTEXTTIDE_DB", ".contexttide/contexttide.sqlite3"))
    return raw if raw.is_absolute() else project_root() / raw

