"""Generic day-by-day sync engine shared by every "one row per calendar_date"
category (daily health metrics, sleep, HRV, training readiness/status, race
predictions, body composition).

Every batch call records to `sync_log` how many dates were *expected*
(requested) vs. *fetched* (successfully queried -- a day with no data is
still a successful query, since sparse days are normal for wellness data;
only actual fetch errors count as a shortfall). Any shortfall raises a
'partial' status with the specific failing dates listed, rather than
letting a partially-failed batch look identical to a clean one.

The resume cursor only advances through the *contiguous* run of
successful dates starting at the front of the batch -- if date 3 of 10
fails, the cursor stops just before date 3, so the next run retries date
3 onward instead of silently skipping past a gap.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from garminconnect import Garmin

from garmin_mcp.db.connection import upsert
from garmin_mcp.db.sync_log import log_sync_run
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, StillCoolingDown
from garmin_mcp.sync.cursor import set_cursor
from garmin_mcp.sync.daily_categories import DailyCategorySpec


def date_range(start: date, end: date) -> list[str]:
    """Inclusive list of 'YYYY-MM-DD' strings from start to end (either order)."""
    step = 1 if end >= start else -1
    days = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(0, days + step, step)]


@dataclass
class SyncResult:
    category: str
    dates: list[str]
    fetched: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped_early: bool = False  # hit a persisted cooldown mid-batch

    @property
    def expected(self) -> int:
        return len(self.dates)

    @property
    def contiguous_success_boundary(self) -> str | None:
        """Last date (in `dates` order) up to which every date succeeded
        with no gap. None if the very first date failed."""
        failed_dates = {d for d, _ in self.failed}
        boundary = None
        for d in self.dates:
            if d in failed_dates:
                break
            boundary = d
        return boundary


def sync_daily_category(
    spec: DailyCategorySpec,
    rate_limited_client: RateLimitedClient,
    garmin: Garmin,
    conn: sqlite3.Connection,
    dates: list[str],
    run_type: str,
) -> SyncResult:
    result = SyncResult(category=spec.category, dates=dates)

    for cdate in dates:
        try:
            raw = rate_limited_client.call(spec.category, spec.fetch, garmin, cdate)
        except StillCoolingDown:
            result.stopped_early = True
            break
        except Exception as exc:  # noqa: BLE001 -- record and keep going; never abort the whole batch silently
            result.failed.append((cdate, repr(exc)))
            continue

        result.fetched += 1
        row = spec.map_row(cdate, raw)
        if row:
            upsert(conn, spec.table, row, ["calendar_date"])
            if spec.extra:
                spec.extra(conn, cdate, raw)

    conn.commit()

    status = "success"
    warning = None
    if result.failed:
        status = "partial"
        shown = [d for d, _ in result.failed[:5]]
        more = "..." if len(result.failed) > 5 else ""
        warning = f"{len(result.failed)}/{result.expected} date(s) failed to fetch: {shown}{more}"
    elif result.stopped_early:
        status = "partial"
        warning = f"stopped early after {result.fetched}/{result.expected} dates due to an active rate-limit cooldown"

    error_message = "; ".join(f"{d}: {e}" for d, e in result.failed) or None

    log_sync_run(
        conn,
        category=spec.category,
        run_type=run_type,
        status=status,
        records_expected=result.expected,
        records_fetched=result.fetched,
        warning=warning,
        error_message=error_message,
        range_start=dates[0] if dates else None,
        range_end=dates[-1] if dates else None,
        cursor_type="date",
    )

    return result


def incremental_sync(
    spec: DailyCategorySpec,
    rate_limited_client: RateLimitedClient,
    garmin: Garmin,
    conn: sqlite3.Connection,
    today: date,
    lookback_days: int = 3,
) -> SyncResult:
    """Sync forward from the last successful cursor through today.

    Re-fetches the cursor date itself (not cursor+1) so a day whose data
    finished landing after we last synced it (e.g. an overnight sleep
    record uploaded the next morning) gets refreshed too.
    """
    from garmin_mcp.sync.cursor import get_cursor

    cursor = get_cursor(conn, spec.category, "forward")
    start = date.fromisoformat(cursor) if cursor else today - timedelta(days=lookback_days)
    dates = date_range(start, today)

    result = sync_daily_category(spec, rate_limited_client, garmin, conn, dates, run_type="incremental_sync")

    boundary = result.contiguous_success_boundary
    if boundary:
        set_cursor(conn, spec.category, "forward", "date", boundary)
    return result


def backfill_batch(
    spec: DailyCategorySpec,
    rate_limited_client: RateLimitedClient,
    garmin: Garmin,
    conn: sqlite3.Connection,
    today: date,
    batch_days: int = 30,
    earliest_date: date = date(2000, 1, 1),
) -> SyncResult | None:
    """Walk one controlled batch further into the past. Returns None once
    `earliest_date` has been reached (nothing left to backfill)."""
    from garmin_mcp.sync.cursor import get_cursor

    cursor = get_cursor(conn, spec.category, "backward")
    end = date.fromisoformat(cursor) - timedelta(days=1) if cursor else today
    if end < earliest_date:
        return None
    start = max(end - timedelta(days=batch_days - 1), earliest_date)

    # Newest-to-oldest within the batch, matching the backward walk direction,
    # so the contiguous-success boundary lands on the oldest date reached.
    dates = date_range(end, start)

    result = sync_daily_category(spec, rate_limited_client, garmin, conn, dates, run_type="backfill")

    boundary = result.contiguous_success_boundary
    if boundary:
        set_cursor(conn, spec.category, "backward", "date", boundary)
    return result
