"""Top-level entry points: `run_sync` (incremental, catch up to today) and
`run_backfill` (one controlled batch further into the past per category).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from garmin_mcp.garmin_client.factory import GarminClients
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient
from garmin_mcp.sync.activities_sync import (
    backfill_activities,
    incremental_sync_activities,
    sync_todays_activities,
)
from garmin_mcp.sync.daily_categories import build_specs
from garmin_mcp.sync.engine import backfill_batch, incremental_sync, sync_daily_category
from garmin_mcp.retry import is_transient


def run_sync(
    conn: sqlite3.Connection,
    clients: GarminClients,
    rate_limited_client: RateLimitedClient,
    today: date | None = None,
) -> dict:
    """Incremental sync: pull everything new since the last successful
    sync, for every category. Never triggered automatically -- callers
    (the MCP 'sync' tool, a cron job, etc.) invoke this explicitly."""
    today = today or date.today()
    results = {}

    results["activities"] = incremental_sync_activities(rate_limited_client, clients, conn)
    for spec in build_specs():
        results[spec.category] = incremental_sync(spec, rate_limited_client, clients, conn, today)

    return results


def run_backfill(
    conn: sqlite3.Connection,
    clients: GarminClients,
    rate_limited_client: RateLimitedClient,
    today: date | None = None,
    batch_days: int = 30,
    earliest_date: date = date(2000, 1, 1),
    categories: set[str] | None = None,
) -> dict:
    """One controlled batch of backfill per category, walking further back
    into history than the bulk export (or a prior backfill run) reached.
    Call repeatedly (e.g. via cron/loop) to walk the full history.

    `categories`, if given, restricts the run to that subset (e.g. just
    {'hrv_daily'}) instead of every category -- useful for topping up one
    thing the bulk export didn't cover without re-walking everything else.
    """
    today = today or date.today()
    results = {}

    if categories is None or "activities" in categories:
        results["activities"] = backfill_activities(rate_limited_client, clients, conn)
    for spec in build_specs():
        if categories is not None and spec.category not in categories:
            continue
        results[spec.category] = backfill_batch(
            spec, rate_limited_client, clients, conn, today, batch_days=batch_days, earliest_date=earliest_date
        )

    return results


def run_todays_activities_sync(
    conn: sqlite3.Connection,
    clients: GarminClients,
    rate_limited_client: RateLimitedClient,
    today: date | None = None,
) -> dict:
    """Evening sync: workouts that began on the local calendar day, plus
    that day's daily health row (for body battery). Sleep and the other
    wellness categories are left to the morning sync.

    The daily health fetch uses its own run_type and never moves the
    forward cursor, so the morning incremental sync and the watchdog are
    unaffected; the morning run re-fetches the day with final totals."""
    today = today or date.today()
    health_spec = next(s for s in build_specs() if s.category == "daily_health_metrics")
    return {
        "activities": sync_todays_activities(rate_limited_client, clients, conn, day=today),
        "daily_health_metrics": sync_daily_category(
            health_spec, rate_limited_client, clients, conn, [today.isoformat()], run_type="evening_body_battery"
        ),
    }


def sync_needs_retry(results: dict, clients: GarminClients) -> str | None:
    """Why a finished sync run should be attempted again, or None.

    Only transient trouble counts (Garmin 5xx, timeouts, connection
    errors): a date that fails the same way every time, or an active
    rate-limit cooldown, would not be fixed by re-running minutes later.
    """
    reasons = []
    if clients.login_error is not None and is_transient(clients.login_error):
        reasons.append(f"garminconnect login failed ({clients.login_error!r})")
    for category, result in results.items():
        count = getattr(result, "transient_failures", 0)
        if count:
            reasons.append(f"{category}: {count} transient failure(s)")
    return "; ".join(reasons) or None
