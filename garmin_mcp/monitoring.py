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
from datetime import date, datetime, timedelta

from garmin_mcp.sync.daily_categories import build_specs
from garmin_mcp.sync.engine import day_has_filled_data


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
    succeed -- except when *no* category has run recently, which means the
    whole job didn't run. That's reported as a single run-level issue
    (category "*") rather than one per category, since every category
    going stale together is one failure, not several."""
    now = now or datetime.utcnow()
    latest = _latest_per_category(conn, run_type)

    fresh = [
        row for cat in categories if (row := latest.get(cat)) is not None
        and now - datetime.fromisoformat(row["started_at"]) <= max_age
    ]
    if categories and not fresh:
        return [_whole_run_issue(run_type, [latest[c] for c in categories if c in latest], now)]

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
                    run_type, category, "stale", f"last run was {_fmt_age(age)} ago (started_at={row['started_at']})"
                )
            )
        elif row["status"] != "success":
            detail = row["warning"] or row["error_message"] or "no detail recorded"
            issues.append(WatchdogIssue(run_type, category, f"status:{row['status']}", detail))

    return issues


def _whole_run_issue(run_type: str, rows: list[sqlite3.Row], now: datetime) -> WatchdogIssue:
    if not rows:
        return WatchdogIssue(run_type, "*", "missing", "no sync_log entry has ever been recorded")
    last_started = max(row["started_at"] for row in rows)
    age = now - datetime.fromisoformat(last_started)
    return WatchdogIssue(
        run_type, "*", "stale", f"job hasn't run in {_fmt_age(age)} (last started_at={last_started})"
    )


def _fmt_age(age: timedelta) -> str:
    # Whole minutes are plenty for a daily job; drop the microseconds noise.
    return str(timedelta(minutes=int(age.total_seconds() // 60)))


def check_sync(conn: sqlite3.Connection, *, max_age: timedelta, now: datetime | None = None) -> list[WatchdogIssue]:
    return check_run_type(conn, run_type="incremental_sync", categories=expected_sync_categories(), max_age=max_age, now=now)


def check_backfill(conn: sqlite3.Connection, *, max_age: timedelta, now: datetime | None = None) -> list[WatchdogIssue]:
    """Backfill's category set is a runtime choice (the launchd plist scopes
    it to a subset), so rather than hardcode that subset here, treat
    "whatever categories have ever logged a backfill run" as the expected
    set -- and flag total absence separately, since that set is empty the
    first time this ever runs too.

    A category whose latest backfill row is `status='complete'` has walked
    all the way back to `--earliest-date` and is dropped from the expected
    set for good -- so the backfill job can be unloaded once it's finished
    without the watchdog treating that as a missed run."""
    conn.row_factory = sqlite3.Row
    seen = conn.execute("SELECT DISTINCT category FROM sync_log WHERE run_type = 'backfill'").fetchall()
    if not seen:
        return [WatchdogIssue("backfill", "*", "missing", "no backfill sync_log entry has ever been recorded")]

    latest = _latest_per_category(conn, "backfill")
    categories = {row["category"] for row in seen if latest[row["category"]]["status"] != "complete"}
    return check_run_type(conn, run_type="backfill", categories=categories, max_age=max_age, now=now)


# Garmin caps personal API keys at a one-year lifetime. The watchdog is
# stateless and runs twice a day, so warn only on milestone days rather than
# every run for a month; once expired, warn on every run.
API_KEY_WARN_DAYS = (30, 14, 7, 3, 2, 1, 0)


def check_api_key_expiry(expires: str | None, *, today: date | None = None) -> list[WatchdogIssue]:
    if not expires:
        return []
    try:
        expiry = date.fromisoformat(expires)
    except ValueError:
        return [WatchdogIssue("config", "api_key", "invalid", f"GARMIN_API_KEY_EXPIRES={expires!r} is not YYYY-MM-DD")]

    days_left = (expiry - (today or date.today())).days
    if days_left < 0:
        return [WatchdogIssue("config", "api_key", "expired", f"Garmin API key expired on {expiry}; create a new one and update .env")]
    if days_left in API_KEY_WARN_DAYS:
        return [WatchdogIssue("config", "api_key", "expiring", f"Garmin API key expires on {expiry} ({days_left} day(s) left); create a new one and update .env")]
    return []


def check_connect_upload(conn: sqlite3.Connection, *, today: date | None = None) -> list[WatchdogIssue]:
    """Flag when last night's sleep is still missing after the sync ran.

    Sync can succeed cleanly while Garmin Connect itself has nothing new,
    because the phone app stopped syncing the watch in the background.
    Sleep is the reliable tell: a night without it almost always means
    the watch's data never reached Connect, and the sync's own status
    can't show that.
    """
    night = (today or date.today()).isoformat()
    if day_has_filled_data(conn, "sleep", night):
        return []
    return [
        WatchdogIssue(
            "connect",
            "sleep",
            "not_uploaded",
            f"no sleep data for the night ending {night}. The watch probably hasn't synced "
            "to Garmin Connect; open the Connect app on your phone, then run garmin-mcp-sync "
            "(or wait for the next scheduled sync).",
        )
    ]
