"""Top-level entry points: `run_sync` (incremental, catch up to today) and
`run_backfill` (one controlled batch further into the past per category).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from garminconnect import Garmin

from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient
from garmin_mcp.sync.activities_sync import backfill_activities, incremental_sync_activities
from garmin_mcp.sync.daily_categories import build_specs
from garmin_mcp.sync.engine import backfill_batch, incremental_sync


def run_sync(
    conn: sqlite3.Connection,
    garmin: Garmin,
    rate_limited_client: RateLimitedClient,
    today: date | None = None,
) -> dict:
    """Incremental sync: pull everything new since the last successful
    sync, for every category. Never triggered automatically -- callers
    (the MCP 'sync' tool, a cron job, etc.) invoke this explicitly."""
    today = today or date.today()
    results = {}

    results["activities"] = incremental_sync_activities(rate_limited_client, garmin, conn)
    for spec in build_specs():
        results[spec.category] = incremental_sync(spec, rate_limited_client, garmin, conn, today)

    return results


def run_backfill(
    conn: sqlite3.Connection,
    garmin: Garmin,
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
        results["activities"] = backfill_activities(rate_limited_client, garmin, conn)
    for spec in build_specs():
        if categories is not None and spec.category not in categories:
            continue
        results[spec.category] = backfill_batch(
            spec, rate_limited_client, garmin, conn, today, batch_days=batch_days, earliest_date=earliest_date
        )

    return results
