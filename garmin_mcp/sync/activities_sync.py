"""Activities sync/backfill via Garmin's offset-paginated activity list.

The list comes from the official Personal API when an API key is set,
otherwise from `garminconnect`; both serve the same endpoint and shape.
Unlike the daily-cadence categories, sync walks it with pure offset
pagination over "most recent first".
That makes it a good fit for *incremental* sync (walk from offset 0 until
we see an activity_id we already have -- "caught up") and a workable, if
approximate, fit for *backfill* (persist how many offset slots we've
consumed and continue from there next time).

Caveat: because new activities appear at offset 0, the offset-to-activity
mapping drifts by however many new activities landed since the last
backfill run. This project doesn't try to correct for that drift with a
date-based method -- idempotent upserts make any resulting overlap harmless, it just means an
already-fetched activity occasionally gets re-fetched. Backfill accounting
(expected/actual, gap warnings) is unaffected either way.

Laps are not in the activity list response; they cost one call per
activity. With an API key that call downloads the original FIT file and
reads its lap messages (garmin_mcp/sync/fit_laps.py). Activities with no
downloadable file (third-party uploads such as Peloton) and runs without a
key fall back to `garminconnect`'s `get_activity_splits`. `fill_missing_laps`
fetches laps for API-sourced activities whose `lap_count` is still NULL
(the marker for "laps never fetched"), a capped number per run so a large
backlog drains over several syncs instead of one long burst.

Deletions: Connect has no "deleted since" feed, so incremental sync infers
them. Every local API-sourced activity newer than the oldest one on the
pages just fetched should have appeared on those pages; any that didn't was
deleted in Connect (e.g. a Peloton duplicate removed by hand) and is
removed locally, laps and all, via ON DELETE CASCADE.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from garmin_mcp.db.connection import upsert
from garmin_mcp.db.sync_log import log_sync_run
from garmin_mcp.garmin_client.factory import GarminClients
from garmin_mcp.garmin_client.rate_limiter import (
    RateLimitedClient,
    RateLimitExceeded,
    StillCoolingDown,
)
from garmin_mcp.sync.cursor import get_cursor, set_cursor
from garmin_mcp.sync.fit_laps import parse_fit_laps

CATEGORY = "activities"

# More missing activities than this in one run looks like a truncated or
# filtered API response rather than hand-deleted duplicates; skip and warn.
MAX_DELETIONS_PER_RUN = 5


def _map_live_activity(act: dict) -> dict | None:
    activity_id = act.get("activityId")
    if activity_id is None:
        return None
    activity_type = (act.get("activityType") or {})
    return {
        "activity_id": activity_id,
        "name": act.get("activityName"),
        "activity_type": activity_type.get("typeKey"),
        # `eventType` (race/training/uncategorized) is a different axis
        # from the bulk export's `sportType` (CYCLING/RUNNING/...) -- there's
        # no equivalent field in this endpoint's response, so it's left
        # unset here rather than populated with the wrong semantics.
        "begin_timestamp_utc_ms": act.get("beginTimestamp"),
        "duration_s": act.get("duration"),
        "elapsed_duration_s": act.get("elapsedDuration"),
        "moving_duration_s": act.get("movingDuration"),
        "distance_m": act.get("distance"),
        "calories": act.get("calories"),
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "avg_speed_mps": act.get("averageSpeed"),
        "max_speed_mps": act.get("maxSpeed"),
        "elevation_gain_m": act.get("elevationGain"),
        "elevation_loss_m": act.get("elevationLoss"),
        "avg_power_w": act.get("avgPower"),
        "max_power_w": act.get("maxPower"),
        "steps": act.get("steps"),
        "location_name": act.get("locationName"),
        "start_latitude": act.get("startLatitude"),
        "start_longitude": act.get("startLongitude"),
        "device_id": act.get("deviceId"),
        "manufacturer": act.get("manufacturer"),
        "source": "api",
    }


def _gmt_to_utc_ms(value: str | None) -> int | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _map_live_lap(activity_id: int, lap_index: int, lap: dict) -> dict:
    start_ms = _gmt_to_utc_ms(lap.get("startTimeGMT"))
    elapsed = lap.get("elapsedDuration")
    return {
        "activity_id": activity_id,
        "lap_index": lap.get("lapIndex") or lap_index,
        "start_time_utc_ms": start_ms,
        "end_time_utc_ms": start_ms + int(elapsed * 1000) if start_ms is not None and elapsed is not None else None,
        "distance_m": lap.get("distance"),
        "duration_s": lap.get("duration"),
        "avg_hr": lap.get("averageHR"),
        "max_hr": lap.get("maxHR"),
        "avg_power_w": lap.get("averagePower"),
        "max_power_w": lap.get("maxPower"),
        "avg_speed_mps": lap.get("averageSpeed"),
        "max_speed_mps": lap.get("maxSpeed"),
        "avg_cadence": lap.get("averageRunCadence", lap.get("averageBikeCadence")),
        "max_cadence": lap.get("maxRunCadence", lap.get("maxBikeCadence")),
        "elevation_gain_m": lap.get("elevationGain"),
        "elevation_loss_m": lap.get("elevationLoss"),
        "calories": lap.get("calories"),
        "intensity_type": lap.get("intensityType"),
        "workout_step_index": lap.get("wktStepIndex"),
        "measurements_json": json.dumps(lap),
    }


@dataclass
class ActivitiesSyncResult:
    pages_fetched: int = 0
    activities_written: int = 0
    laps_activities_filled: int = 0
    laps_written: int = 0
    deleted_activity_ids: list[int] = None
    reached_known_activity: bool = False
    reached_end_of_history: bool = False
    stopped_early: bool = False
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []
        if self.deleted_activity_ids is None:
            self.deleted_activity_ids = []


def _activity_list_client(clients: GarminClients):
    return clients.personal or clients.garmin


def _fetch_laps(clients: GarminClients, activity_id: int) -> list[dict]:
    if clients.personal is not None:
        payload = clients.personal.download_activity_fit(activity_id)
        if payload is not None:
            return parse_fit_laps(activity_id, payload)
    splits = clients.garmin.get_activity_splits(activity_id)
    laps = (splits or {}).get("lapDTOs") or []
    return [_map_live_lap(activity_id, i, lap) for i, lap in enumerate(laps, start=1)]


def fill_missing_laps(
    rate_limited_client: RateLimitedClient,
    clients: GarminClients,
    conn: sqlite3.Connection,
    result: ActivitiesSyncResult,
    max_activities: int = 40,
) -> None:
    """Fetch laps for API-sourced activities that have never had them
    fetched (lap_count IS NULL), newest first, up to `max_activities`."""
    pending = conn.execute(
        """
        SELECT activity_id FROM activities
        WHERE source = 'api' AND lap_count IS NULL
        ORDER BY begin_timestamp_utc_ms DESC
        LIMIT ?
        """,
        (max_activities,),
    ).fetchall()

    for (activity_id,) in pending:
        try:
            laps = rate_limited_client.call(CATEGORY, _fetch_laps, clients, activity_id)
        except (StillCoolingDown, RateLimitExceeded):
            result.stopped_early = True
            break
        except Exception as exc:  # noqa: BLE001 -- one bad activity shouldn't block the rest; retried next run
            result.warnings.append(f"laps for activity {activity_id} failed: {exc!r}")
            continue

        for lap in laps:
            upsert(conn, "activity_laps", lap, ["activity_id", "lap_index"])
        conn.execute("UPDATE activities SET lap_count = ? WHERE activity_id = ?", (len(laps), activity_id))
        conn.commit()
        result.laps_activities_filled += 1
        result.laps_written += len(laps)


def remove_deleted_activities(
    conn: sqlite3.Connection,
    seen_ids: set[int],
    oldest_seen_ms: int,
    result: ActivitiesSyncResult,
) -> None:
    """Delete local API-sourced activities that started after
    `oldest_seen_ms` but weren't in the freshly fetched list. Strictly
    after: another activity sharing the boundary timestamp may sit on the
    next, unfetched page. Bulk-export rows are never touched."""
    missing = conn.execute(
        """
        SELECT activity_id, name FROM activities
        WHERE source = 'api' AND begin_timestamp_utc_ms > ?
        """,
        (oldest_seen_ms,),
    ).fetchall()
    missing = [(aid, name) for aid, name in missing if aid not in seen_ids]
    if not missing:
        return
    if len(missing) > MAX_DELETIONS_PER_RUN:
        result.warnings.append(
            f"{len(missing)} recent activities missing from Garmin's list; over the "
            f"limit of {MAX_DELETIONS_PER_RUN}, so none were deleted locally"
        )
        return
    conn.executemany("DELETE FROM activities WHERE activity_id = ?", [(aid,) for aid, _ in missing])
    conn.commit()
    result.deleted_activity_ids.extend(aid for aid, _ in missing)


def incremental_sync_activities(
    rate_limited_client: RateLimitedClient,
    clients: GarminClients,
    conn: sqlite3.Connection,
    page_size: int = 50,
    max_pages: int = 20,
) -> ActivitiesSyncResult:
    """Walk newest-first from offset 0 until an already-known activity_id
    is seen (caught up) or max_pages is hit."""
    result = ActivitiesSyncResult()
    start = 0
    seen_ids: set[int] = set()
    oldest_seen_ms: int | None = None

    for _ in range(max_pages):
        try:
            page = rate_limited_client.call(CATEGORY, _activity_list_client(clients).get_activities, start, page_size)
        except StillCoolingDown:
            result.stopped_early = True
            break

        result.pages_fetched += 1
        if not page:
            result.reached_end_of_history = True
            break

        if 0 < len(page) < page_size:
            result.warnings.append(
                f"page at offset {start} returned {len(page)}/{page_size} activities "
                "without an explicit end-of-history signal"
            )

        for act in page:
            row = _map_live_activity(act)
            if not row:
                continue
            seen_ids.add(row["activity_id"])
            if row["begin_timestamp_utc_ms"] is not None:
                oldest_seen_ms = min(oldest_seen_ms or row["begin_timestamp_utc_ms"], row["begin_timestamp_utc_ms"])
            existing = conn.execute(
                "SELECT 1 FROM activities WHERE activity_id = ?", (row["activity_id"],)
            ).fetchone()
            if existing:
                result.reached_known_activity = True
                continue
            upsert(conn, "activities", row, ["activity_id"])
            result.activities_written += 1

        conn.commit()

        if result.reached_known_activity or len(page) < page_size:
            break
        start += page_size

    if not result.stopped_early:
        if oldest_seen_ms is not None:
            remove_deleted_activities(conn, seen_ids, oldest_seen_ms, result)
        fill_missing_laps(rate_limited_client, clients, conn, result)

    status = "success"
    warning = "; ".join(result.warnings) or None
    if result.stopped_early:
        status = "partial"
        warning = (warning + "; " if warning else "") + "stopped early: active rate-limit cooldown"
    elif result.warnings:
        status = "partial"
    if result.deleted_activity_ids:
        note = f"removed {len(result.deleted_activity_ids)} activity(ies) deleted in Garmin Connect: {result.deleted_activity_ids}"
        warning = (warning + "; " if warning else "") + note

    log_sync_run(
        conn,
        category=CATEGORY,
        run_type="incremental_sync",
        status=status,
        records_expected=result.pages_fetched * page_size,
        records_fetched=result.activities_written,
        warning=warning,
        cursor_type="offset",
    )
    return result


def backfill_activities(
    rate_limited_client: RateLimitedClient,
    clients: GarminClients,
    conn: sqlite3.Connection,
    page_size: int = 50,
    max_pages: int = 5,
) -> ActivitiesSyncResult:
    """Continue walking backward into history from the persisted offset
    cursor, `max_pages` pages at a time (a "controlled batch")."""
    result = ActivitiesSyncResult()
    cursor = get_cursor(conn, CATEGORY, "backward")
    start = int(cursor) if cursor else 0

    for _ in range(max_pages):
        try:
            page = rate_limited_client.call(CATEGORY, _activity_list_client(clients).get_activities, start, page_size)
        except StillCoolingDown:
            result.stopped_early = True
            break

        result.pages_fetched += 1
        if not page:
            result.reached_end_of_history = True
            break

        if 0 < len(page) < page_size:
            result.warnings.append(
                f"page at offset {start} returned {len(page)}/{page_size} activities "
                "without an explicit end-of-history signal"
            )

        for act in page:
            row = _map_live_activity(act)
            if row:
                upsert(conn, "activities", row, ["activity_id"])
                result.activities_written += 1

        conn.commit()
        start += page_size

        if len(page) < page_size:
            result.reached_end_of_history = True
            break

    set_cursor(conn, CATEGORY, "backward", "offset", str(start))

    status = "success"
    warning = "; ".join(result.warnings) or None
    if result.stopped_early:
        status = "partial"
        warning = (warning + "; " if warning else "") + "stopped early: active rate-limit cooldown"
    elif result.warnings:
        status = "partial"

    log_sync_run(
        conn,
        category=CATEGORY,
        run_type="backfill",
        status=status,
        records_expected=result.pages_fetched * page_size,
        records_fetched=result.activities_written,
        warning=warning,
        cursor_type="offset",
    )
    return result
