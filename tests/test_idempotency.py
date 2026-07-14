"""Tests for sync-state / idempotency: re-running an import or a sync must
never create duplicate rows, and interrupted/partial batches must resume
from the right place rather than silently skipping or re-processing
everything. This is the other of the two failure modes the project is
explicitly built to avoid (the other being rate-limit backoff, covered in
test_rate_limiter.py).
"""

from __future__ import annotations

import json
import zipfile
from datetime import date

import pytest

from garmin_mcp.bulk_import.runner import import_export
from garmin_mcp.db.connection import init_db, upsert
from garmin_mcp.sync.cursor import get_cursor, set_cursor
from garmin_mcp.sync.daily_categories import DailyCategorySpec
from garmin_mcp.sync.engine import date_range, sync_daily_category


# ---------------------------------------------------------------------
# Generic upsert() helper
# ---------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def test_upsert_does_not_duplicate_on_natural_key_conflict(conn):
    row = {"calendar_date": "2026-01-01", "total_steps": 1000, "source": "api"}
    upsert(conn, "daily_health_metrics", row, ["calendar_date"])
    upsert(conn, "daily_health_metrics", {**row, "total_steps": 2000}, ["calendar_date"])

    rows = conn.execute("SELECT * FROM daily_health_metrics").fetchall()
    assert len(rows) == 1
    assert rows[0]["total_steps"] == 2000  # latest write wins


def test_upsert_partial_row_does_not_clobber_other_columns(conn):
    upsert(
        conn,
        "training_status",
        {"calendar_date": "2026-01-01", "vo2max_running": 45.0, "source": "csv_export"},
        ["calendar_date"],
    )
    # a second source file contributes a different subset of columns for the same date
    upsert(
        conn,
        "training_status",
        {"calendar_date": "2026-01-01", "endurance_score": 4000, "source": "csv_export"},
        ["calendar_date"],
    )
    row = conn.execute("SELECT * FROM training_status WHERE calendar_date = '2026-01-01'").fetchone()
    assert row["vo2max_running"] == 45.0  # untouched by the second upsert
    assert row["endurance_score"] == 4000


def test_activity_hr_zones_upsert_idempotent(conn):
    upsert(
        conn, "activities",
        {"activity_id": 1, "begin_timestamp_utc_ms": 0, "source": "api"},
        ["activity_id"],
    )
    for _ in range(3):
        upsert(conn, "activity_hr_zones", {"activity_id": 1, "zone_number": 2, "seconds_in_zone": 60}, ["activity_id", "zone_number"])
    rows = conn.execute("SELECT * FROM activity_hr_zones").fetchall()
    assert len(rows) == 1
    assert rows[0]["seconds_in_zone"] == 60


# ---------------------------------------------------------------------
# Bulk import idempotency against a minimal synthetic export archive
# ---------------------------------------------------------------------


def _build_fake_export(tmp_path) -> "Path":
    activities = [
        {
            "summarizedActivitiesExport": [
                {
                    "activityId": 111,
                    "name": "Morning Run",
                    "activityType": "running",
                    "sportType": "RUNNING",
                    "beginTimestamp": 1700000000000,
                    "startTimeLocal": 1699996400000,
                    "duration": 1800000.0,
                    "distance": 500000.0,
                    "calories": 400.0,
                    "avgHr": 140.0,
                    "hrTimeInZone_2": 600.0,
                    "splits": [
                        {
                            "startTimeGMT": 1700000000000,
                            "endTimeGMT": 1700000600000,
                            "measurements": [
                                {"fieldEnum": "SUM_DISTANCE", "value": 100000.0, "unitEnum": "CENTIMETER"},
                                {"fieldEnum": "WEIGHTED_MEAN_HEARTRATE", "value": 138.0, "unitEnum": "BPM"},
                            ],
                        }
                    ],
                }
            ]
        }
    ]
    uds = [
        {
            "calendarDate": "2023-11-14",
            "totalSteps": 8000,
            "restingHeartRate": 50,
            "allDayStress": {"aggregatorList": [{"type": "TOTAL", "averageStressLevel": 20}]},
            "bodyBattery": {"chargedValue": 50, "drainedValue": 40, "bodyBatteryStatList": []},
            "respiration": {},
        }
    ]

    zip_path = tmp_path / "fake_export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "DI_CONNECT/DI-Connect-Fitness/user_1001_summarizedActivities.json",
            json.dumps(activities),
        )
        zf.writestr(
            "DI_CONNECT/DI-Connect-Aggregator/UDSFile_2023-11-14_2023-11-14.json",
            json.dumps(uds),
        )
    return zip_path


def test_bulk_import_is_idempotent(tmp_path):
    zip_path = _build_fake_export(tmp_path)
    db_path = tmp_path / "garmin.db"

    report1 = import_export(zip_path, db_path)
    assert report1.category("activities").rows_written == 1

    report2 = import_export(zip_path, db_path)
    assert report2.category("activities").rows_written == 1  # processed again, but...

    from garmin_mcp.db.connection import connect

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM activity_laps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM activity_hr_zones").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM daily_health_metrics").fetchone()[0] == 1
        # values reflect the (identical) re-import, not doubled
        steps = conn.execute("SELECT total_steps FROM daily_health_metrics").fetchone()[0]
        assert steps == 8000
    finally:
        conn.close()


# ---------------------------------------------------------------------
# sync_cursor round trip
# ---------------------------------------------------------------------


def test_sync_cursor_upsert_round_trip(conn):
    set_cursor(conn, "sleep", "forward", "date", "2026-01-01")
    assert get_cursor(conn, "sleep", "forward") == "2026-01-01"

    set_cursor(conn, "sleep", "forward", "date", "2026-01-05")
    assert get_cursor(conn, "sleep", "forward") == "2026-01-05"
    assert conn.execute("SELECT COUNT(*) FROM sync_cursor").fetchone()[0] == 1

    # 'backward' direction for the same category is independent
    set_cursor(conn, "sleep", "backward", "date", "2020-01-01")
    assert get_cursor(conn, "sleep", "backward") == "2020-01-01"
    assert get_cursor(conn, "sleep", "forward") == "2026-01-05"


def test_set_cursor_is_durable_without_a_later_commit(tmp_path):
    """Regression test: a cursor write must survive on its own even when
    it's the very last thing a process does before exiting (e.g. a
    single-category backfill run) -- it must not depend on some other,
    later operation happening to commit the same transaction."""
    db_path = tmp_path / "durable.db"
    conn = init_db(db_path)
    set_cursor(conn, "hrv_daily", "backward", "date", "2026-06-14")
    conn.close()  # no explicit commit() here -- set_cursor must have self-committed

    reopened = init_db(db_path)
    assert get_cursor(reopened, "hrv_daily", "backward") == "2026-06-14"
    reopened.close()


# ---------------------------------------------------------------------
# Daily sync engine: partial-failure resume behavior
# ---------------------------------------------------------------------


def _spec_with_fetch(fetch_fn) -> DailyCategorySpec:
    return DailyCategorySpec(
        category="sleep",
        table="sleep",
        fetch=fetch_fn,
        map_row=lambda cdate, raw: {"calendar_date": cdate, "overall_score": raw["score"], "source": "api"},
    )


class DummyRateLimitedClient:
    """Bypasses real rate limiting -- just calls straight through -- so the
    engine's own resume/idempotency logic can be tested in isolation."""

    def call(self, category, func, *args, **kwargs):
        return func(*args, **kwargs)


def test_sync_daily_category_advances_cursor_only_through_contiguous_success(conn):
    dates = date_range(date(2026, 1, 1), date(2026, 1, 5))

    def fetch(garmin, cdate):
        if cdate == "2026-01-03":
            raise RuntimeError("simulated transient failure")
        return {"score": 80}

    spec = _spec_with_fetch(fetch)
    result = sync_daily_category(spec, DummyRateLimitedClient(), None, conn, dates, run_type="incremental_sync")

    assert result.failed == [("2026-01-03", "RuntimeError('simulated transient failure')")]
    # only 2026-01-01 and 2026-01-02 succeeded contiguously before the gap
    assert result.contiguous_success_boundary == "2026-01-02"

    # rows exist for every date that succeeded, including ones after the gap
    written = {r["calendar_date"] for r in conn.execute("SELECT calendar_date FROM sleep").fetchall()}
    assert written == {"2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"}

    log_row = conn.execute(
        "SELECT * FROM sync_log WHERE category = 'sleep' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert log_row["status"] == "partial"
    assert log_row["records_expected"] == 5
    assert log_row["records_fetched"] == 4  # 4 of 5 dates were successfully queried


def test_sync_daily_category_rerun_is_idempotent(conn):
    dates = date_range(date(2026, 2, 1), date(2026, 2, 3))

    def fetch(garmin, cdate):
        return {"score": 90}

    spec = _spec_with_fetch(fetch)
    sync_daily_category(spec, DummyRateLimitedClient(), None, conn, dates, run_type="incremental_sync")
    sync_daily_category(spec, DummyRateLimitedClient(), None, conn, dates, run_type="incremental_sync")

    count = conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0]
    assert count == 3  # not 6 -- re-running never duplicates rows
