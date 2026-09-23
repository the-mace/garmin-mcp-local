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

Incremental sync also walks back through *local empty gaps*: days the API
previously returned nothing for (watch not uploaded to Connect) leave
either no row or a null shell. When the phone later uploads those nights,
the forward cursor alone would skip them. Each incremental run therefore
extends its start date back through consecutive empty/missing days until
it hits a filled day (or GAP_REFETCH_MAX_DAYS), then re-fetches that range
through today.
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

# How far back incremental sync may walk to refill empty days that later
# land in Connect (e.g. phone app backgrounded during travel). Cap prevents
# unbounded re-fetch when a category is legitimately sparse for weeks.
GAP_REFETCH_MAX_DAYS = 21

# A day counts as "filled" if any of these columns is non-NULL. Empty API
# shells (calendar_date only / all metrics null) and missing rows are gaps.
_FILLED_ANY_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_health_metrics": (
        "resting_hr",
        "total_steps",
        "avg_stress",
        "body_battery_highest",
        "min_hr",
    ),
    "sleep": (
        "overall_score",
        "deep_sleep_s",
        "light_sleep_s",
        "rem_sleep_s",
        "sleep_start_utc_ms",
    ),
    "hrv_daily": ("last_night_avg", "weekly_avg", "status"),
    "training_readiness": ("score", "level"),
    "training_status": ("training_status", "vo2max_running", "acute_load", "fitness_trend"),
    "race_predictions": (
        "time_5k_s",
        "time_10k_s",
        "time_half_marathon_s",
        "time_marathon_s",
    ),
    "body_composition": ("weight_g", "bmi", "body_fat_percent", "muscle_mass_g"),
}


def date_range(start: date, end: date) -> list[str]:
    """Inclusive list of 'YYYY-MM-DD' strings from start to end (either order)."""
    step = 1 if end >= start else -1
    days = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(0, days + step, step)]


def day_has_filled_data(conn: sqlite3.Connection, table: str, cdate: str) -> bool:
    """True when `table` has a row for `cdate` with at least one real metric.

    Missing rows and null shells (API returned empty, cursor still advanced)
    count as gaps so incremental sync will re-request them later.
    """
    cols = _FILLED_ANY_COLUMNS.get(table)
    # table names come only from DailyCategorySpec, never user input
    if not cols:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE calendar_date = ?",
            (cdate,),
        ).fetchone()
        return row is not None
    conditions = " OR ".join(f"{c} IS NOT NULL" for c in cols)
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE calendar_date = ? AND ({conditions})",
        (cdate,),
    ).fetchone()
    return row is not None


def gap_refetch_start(
    conn: sqlite3.Connection,
    table: str,
    cursor_start: date,
    today: date,
    *,
    max_gap_days: int = GAP_REFETCH_MAX_DAYS,
) -> date:
    """Compute incremental-sync start so empty local days get another chance.

    Walks **backward from the cursor** through consecutive local gaps
    (missing row or null shell) until the previous day is filled, or until
    ``today - max_gap_days``. Does not jump to arbitrary empty days deeper
    in history — only the trailing empty run the forward cursor already
    walked through (the phone-backgrounded-for-N-days case).

    Example: cursor 7/31, 7/31 may be filled or empty, 7/27–30 empty, 7/26
    filled → returns 7/27 so those nights are requested again.
    """
    if max_gap_days < 0:
        raise ValueError("max_gap_days must be >= 0")
    floor = today - timedelta(days=max_gap_days)
    start = cursor_start
    if start > today:
        start = today
    if start < floor:
        start = floor

    # If the cursor day itself is still a gap, include it (already in start)
    # and keep walking back through older consecutive gaps.
    while start > floor:
        prev = start - timedelta(days=1)
        if day_has_filled_data(conn, table, prev.isoformat()):
            break
        start = prev

    return start


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
    max_gap_days: int = GAP_REFETCH_MAX_DAYS,
) -> SyncResult:
    """Sync forward from the last successful cursor through today.

    Re-fetches the cursor date itself (not cursor+1) so a day whose data
    finished landing after we last synced it (e.g. an overnight sleep
    record uploaded the next morning) gets refreshed too.

    Also walks back through local empty/missing days (up to
    ``max_gap_days``) until a filled day is found, so delayed Connect
    uploads after a phone/watch sync gap are not stranded past the cursor.
    """
    from garmin_mcp.sync.cursor import get_cursor

    cursor = get_cursor(conn, spec.category, "forward")
    cursor_start = (
        date.fromisoformat(cursor) if cursor else today - timedelta(days=lookback_days)
    )
    start = gap_refetch_start(
        conn,
        spec.table,
        cursor_start,
        today,
        max_gap_days=max_gap_days,
    )
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
    `earliest_date` has been reached (nothing left to backfill), and logs a
    `status='complete'` row so the watchdog knows this category is done
    rather than stalled."""
    from garmin_mcp.sync.cursor import get_cursor

    cursor = get_cursor(conn, spec.category, "backward")
    end = date.fromisoformat(cursor) - timedelta(days=1) if cursor else today
    if end < earliest_date:
        log_sync_run(
            conn,
            category=spec.category,
            run_type="backfill",
            status="complete",
            cursor_type="date",
            range_start=earliest_date.isoformat(),
            range_end=cursor,
        )
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
