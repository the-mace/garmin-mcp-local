"""Watchdog checks over `sync_log`, the project's existing audit trail.

Exists because a scheduled run can fail silently two different ways that a
"catch the exception and email" wrapper alone won't see:

- it never runs at all (launchd agent unloaded, laptop asleep at 06:00,
  etc.) -- there's no process to catch anything in;
- it runs and exits 0, but a per-date fetch failed internally and was
  recorded as `status='partial'`/`'failed'`/`'rate_limited'` without
  raising (see `garmin_mcp/sync/engine.py` -- failures are logged and
  swallowed so one bad date doesn't abort the whole batch).

Both are only visible by reading `sync_log` itself, which is what this
module does.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from garmin_mcp.sync.daily_categories import build_specs


@dataclass(frozen=True)
class WatchdogIssue:
    run_type: str
    category: str
    problem: str  # "missing" | "stale" | "status:<status>"
    detail: str

    def __str__(self) -> str:
        return f"[{self.run_type}/{self.category}] {self.problem}: {self.detail}"


def expected_sync_categories() -> set[str]:
    """Every category `run_sync` always covers (see sync/runner.py) --
    derived from the same spec list sync itself uses, so this can't drift
    out of sync with what actually gets fetched."""
    return {"activities"} | {spec.category for spec in build_specs()}


def _latest_per_category(conn: sqlite3.Connection, run_type: str) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT category, status, started_at, warning, error_message
        FROM sync_log s
        WHERE run_type = ?
          AND started_at = (
              SELECT MAX(started_at) FROM sync_log s2
              WHERE s2.category = s.category AND s2.run_type = ?
          )
        """,
        (run_type, run_type),
    ).fetchall()
    return {row["category"]: row for row in rows}


def check_run_type(
    conn: sqlite3.Connection,
    *,
    run_type: str,
    categories: set[str],
    max_age: timedelta,
    now: datetime | None = None,
) -> list[WatchdogIssue]:
    """One issue per category that's either never logged a run, gone
    stale (older than `max_age`), or whose most recent run didn't
    succeed."""
    now = now or datetime.utcnow()
    latest = _latest_per_category(conn, run_type)
    issues: list[WatchdogIssue] = []

    for category in sorted(categories):
        row = latest.get(category)
        if row is None:
            issues.append(
                WatchdogIssue(run_type, category, "missing", "no sync_log entry has ever been recorded")
            )
            continue

        started_at = datetime.fromisoformat(row["started_at"])
        age = now - started_at
        if age > max_age:
            issues.append(
                WatchdogIssue(
                    run_type, category, "stale", f"last run was {age} ago (started_at={row['started_at']})"
                )
            )
        elif row["status"] != "success":
            detail = row["warning"] or row["error_message"] or "no detail recorded"
            issues.append(WatchdogIssue(run_type, category, f"status:{row['status']}", detail))

    return issues


def check_sync(conn: sqlite3.Connection, *, max_age: timedelta, now: datetime | None = None) -> list[WatchdogIssue]:
    return check_run_type(conn, run_type="incremental_sync", categories=expected_sync_categories(), max_age=max_age, now=now)


def check_backfill(conn: sqlite3.Connection, *, max_age: timedelta, now: datetime | None = None) -> list[WatchdogIssue]:
    """Backfill's category set is a runtime choice (the launchd plist scopes
    it to a subset), so rather than hardcode that subset here, treat
    "whatever categories have ever logged a backfill run" as the expected
    set -- and flag total absence separately, since that set is empty the
    first time this ever runs too."""
    conn.row_factory = sqlite3.Row
    seen = conn.execute("SELECT DISTINCT category FROM sync_log WHERE run_type = 'backfill'").fetchall()
    if not seen:
        return [WatchdogIssue("backfill", "*", "missing", "no backfill sync_log entry has ever been recorded")]

    categories = {row["category"] for row in seen}
    return check_run_type(conn, run_type="backfill", categories=categories, max_age=max_age, now=now)
