"""Environment-based configuration. No credentials ever live in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repo root (parent of the garmin_mcp package), not the process's current
# working directory -- a scheduled/launchd invocation may have a cwd
# unrelated to this project (or one it can't even resolve, e.g. one
# sitting inside a TCC-protected folder without its own grant), so both
# finding .env and resolving relative paths from it must not depend on cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(_REPO_ROOT / ".env")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (_REPO_ROOT / path).resolve()


@dataclass(frozen=True)
class Config:
    garmin_email: str | None
    garmin_password: str | None
    token_store: Path
    db_path: Path
    min_request_interval_seconds: float
    max_retries: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            garmin_email=os.environ.get("GARMIN_EMAIL") or None,
            garmin_password=os.environ.get("GARMIN_PASSWORD") or None,
            token_store=_resolve_path(os.environ.get("GARMIN_TOKEN_STORE", "./.garminconnect")),
            db_path=_resolve_path(os.environ.get("GARMIN_DB_PATH", "./data/garmin.db")),
            min_request_interval_seconds=float(
                os.environ.get("GARMIN_MIN_REQUEST_INTERVAL_SECONDS", "1.5")
            ),
            max_retries=int(os.environ.get("GARMIN_MAX_RETRIES", "5")),
        )


def get_config() -> Config:
    return Config.from_env()
