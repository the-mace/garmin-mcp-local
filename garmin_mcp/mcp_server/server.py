"""Garmin MCP server: read-only query tools backed by the local SQLite
cache, plus one explicit `sync` tool that hits the live Garmin API.

Normal queries never touch the network -- they only read `garmin.db`.
Nothing here auto-triggers a sync on a query; the user (or their agent)
must explicitly call `sync_now` / `backfill_batch`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from mcp.server.fastmcp import FastMCP

from garmin_mcp.config import get_config
from garmin_mcp.db.connection import connect

mcp = FastMCP("garmin-mcp-local")

_READ_ONLY_SQL_PREFIXES = ("select", "with", "pragma table_info", "explain")


def _get_conn() -> sqlite3.Connection:
    config = get_config()
    return connect(config.db_path)


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _date_to_epoch_ms(d: str) -> int:
    return int(datetime.combine(date.fromisoformat(d), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)


@mcp.tool()
def list_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    sport_type: str | None = None,
    limit: int = 50,
) -> str:
    """List activities from the local cache, most recent first.

    start_date/end_date: 'YYYY-MM-DD', inclusive, optional.
    activity_type: exact match against Garmin's granular activity_type
        (e.g. 'road_biking', 'gravel_cycling', 'indoor_cycling', 'treadmill_running'), optional.
    sport_type: exact match against Garmin's broader sport_type grouping
        (e.g. 'CYCLING', 'RUNNING', 'WALKING') -- prefer this over activity_type
        for questions like "how many bike rides" or "how many runs", since it
        groups all the granular variants (road/gravel/indoor cycling, etc.)
        together in one filter instead of requiring you to enumerate and
        manually classify a mixed activity list yourself. Optional.
    """
    conn = _get_conn()
    try:
        clauses = []
        params: list = []
        if start_date:
            clauses.append("begin_timestamp_utc_ms >= ?")
            params.append(_date_to_epoch_ms(start_date))
        if end_date:
            clauses.append("begin_timestamp_utc_ms < ?")
            params.append(_date_to_epoch_ms(end_date) + 86400_000)
        if activity_type:
            clauses.append("activity_type = ?")
            params.append(activity_type)
        if sport_type:
            clauses.append("sport_type = ?")
            params.append(sport_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur = conn.execute(
            f"""
            SELECT activity_id, name, activity_type, sport_type, begin_timestamp_utc_ms,
                   duration_s, distance_m, calories, avg_hr, max_hr, avg_power_w,
                   training_stress_score, aerobic_training_effect, elevation_gain_m
            FROM activities {where}
            ORDER BY begin_timestamp_utc_ms DESC
            LIMIT ?
            """,
            params,
        )
        return json.dumps(_rows_to_dicts(cur), indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_activity_detail(activity_id: int) -> str:
    """Full detail for one activity: summary row + laps + HR/power zones + gear."""
    conn = _get_conn()
    try:
        activity = conn.execute("SELECT * FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
        if not activity:
            return json.dumps({"error": f"activity_id {activity_id} not found in local cache"})
        laps = _rows_to_dicts(conn.execute(
            "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index", (activity_id,)
        ))
        hr_zones = _rows_to_dicts(conn.execute(
            "SELECT * FROM activity_hr_zones WHERE activity_id = ? ORDER BY zone_number", (activity_id,)
        ))
        power_zones = _rows_to_dicts(conn.execute(
            "SELECT * FROM activity_power_zones WHERE activity_id = ? ORDER BY zone_number", (activity_id,)
        ))
        gear = _rows_to_dicts(conn.execute(
            """
            SELECT g.* FROM gear g
            JOIN activity_gear ag ON ag.gear_id = g.gear_id
            WHERE ag.activity_id = ?
            """,
            (activity_id,),
        ))
        return json.dumps(
            {"activity": dict(activity), "laps": laps, "hr_zones": hr_zones, "power_zones": power_zones, "gear": gear},
            indent=2,
        )
    finally:
        conn.close()


@mcp.tool()
def get_daily_health_metrics(start_date: str, end_date: str) -> str:
    """Daily steps/HR/stress/body battery/SpO2/respiration for a date range (inclusive, 'YYYY-MM-DD')."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT * FROM daily_health_metrics
            WHERE calendar_date BETWEEN ? AND ?
            ORDER BY calendar_date
            """,
            (start_date, end_date),
        )
        return json.dumps(_rows_to_dicts(cur), indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_sleep(start_date: str, end_date: str) -> str:
    """Nightly sleep stages + sleep score breakdown for a date range (inclusive, 'YYYY-MM-DD')."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM sleep WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
            (start_date, end_date),
        )
        return json.dumps(_rows_to_dicts(cur), indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_training_trends(start_date: str, end_date: str) -> str:
    """Training status, readiness, VO2max, load (ACWR), endurance/hill scores,
    and race predictions for a date range (inclusive, 'YYYY-MM-DD')."""
    conn = _get_conn()
    try:
        result = {}
        for table in ("training_status", "training_readiness", "race_predictions", "hrv_daily"):
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
                (start_date, end_date),
            )
            result[table] = _rows_to_dicts(cur)
        return json.dumps(result, indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_sync_status() -> str:
    """Recent sync_log entries -- what's been fetched, what failed/was rate-limited,
    and the current resume cursors for every category. Check this before trusting
    'no data' as a real gap vs. a not-yet-synced range."""
    conn = _get_conn()
    try:
        log = _rows_to_dicts(conn.execute(
            "SELECT * FROM sync_log ORDER BY started_at DESC LIMIT 30"
        ))
        cursors = _rows_to_dicts(conn.execute("SELECT * FROM sync_cursor ORDER BY category, direction"))
        return json.dumps({"recent_sync_log": log, "cursors": cursors}, indent=2)
    finally:
        conn.close()


@mcp.tool()
def execute_sql(query: str) -> str:
    """Run an ad hoc read-only SQL query against the local cache.
    Only SELECT/WITH/EXPLAIN/PRAGMA table_info statements are allowed."""
    normalized = query.strip().lower()
    if not normalized.startswith(_READ_ONLY_SQL_PREFIXES):
        return json.dumps({"error": "only SELECT/WITH/EXPLAIN/PRAGMA table_info statements are allowed"})
    conn = _get_conn()
    try:
        cur = conn.execute(query)
        return json.dumps(_rows_to_dicts(cur), indent=2, default=str)
    except sqlite3.Error as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def sync_now() -> str:
    """Explicitly hit the live Garmin API to pull new data since the last
    successful sync, for every category. Rate-limited; respects any active
    cooldown from a previous 429. Never called automatically by other tools."""
    from garmin_mcp.garmin_client.factory import build_client
    from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
    from garmin_mcp.sync.runner import run_sync

    config = get_config()
    conn = _get_conn()
    try:
        garmin = build_client(config)
        rate_limited_client = RateLimitedClient(
            conn,
            RateLimiterConfig(
                min_request_interval_seconds=config.min_request_interval_seconds,
                max_retries=config.max_retries,
            ),
        )
        results = run_sync(conn, garmin, rate_limited_client)
        summary = {
            category: {
                "expected": getattr(r, "expected", None),
                "fetched": getattr(r, "fetched", getattr(r, "activities_written", None)),
            }
            for category, r in results.items()
        }
        return json.dumps(summary, indent=2)
    finally:
        conn.close()


@mcp.tool()
def backfill_batch_now(batch_days: int = 30, earliest_date: str = "2000-01-01", categories: list[str] | None = None) -> str:
    """Run one controlled batch of API-driven backfill per category, walking
    further back into history than the bulk export (or a prior backfill run)
    reached. Call repeatedly to walk the full history without one giant fetch.

    `categories`, if given, restricts the batch to that subset (e.g.
    ["hrv_daily"]) instead of every category -- one of: activities,
    daily_health_metrics, sleep, hrv_daily, training_readiness,
    training_status, race_predictions, body_composition."""
    from garmin_mcp.garmin_client.factory import build_client
    from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
    from garmin_mcp.sync.runner import run_backfill

    config = get_config()
    conn = _get_conn()
    try:
        garmin = build_client(config)
        rate_limited_client = RateLimitedClient(
            conn,
            RateLimiterConfig(
                min_request_interval_seconds=config.min_request_interval_seconds,
                max_retries=config.max_retries,
            ),
        )
        results = run_backfill(
            conn,
            garmin,
            rate_limited_client,
            batch_days=batch_days,
            earliest_date=date.fromisoformat(earliest_date),
            categories=set(categories) if categories else None,
        )
        summary = {
            category: (
                None
                if r is None
                else {
                    "expected": getattr(r, "expected", None),
                    "fetched": getattr(r, "fetched", getattr(r, "activities_written", None)),
                }
            )
            for category, r in results.items()
        }
        return json.dumps(summary, indent=2)
    finally:
        conn.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
