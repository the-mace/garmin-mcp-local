"""Tests for the watchdog checks in garmin_mcp/monitoring.py: catching a
scheduled run that never happened, went stale, or completed without
raising but logged a non-success status. This is the failure mode neither
test_idempotency.py nor test_rate_limiter.py covers -- a per-date fetch
error is swallowed and recorded to `sync_log` rather than propagated (see
garmin_mcp/sync/engine.py), so nothing short of reading `sync_log` itself
notices it happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from garmin_mcp.db.connection import init_db
from garmin_mcp.monitoring import check_backfill, check_run_type, check_sync, expected_sync_categories

MAX_AGE = timedelta(hours=27)
NOW = datetime(2026, 7, 14, 8, 0, 0)


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def _insert_log(conn, *, category, run_type, status, started_at):
    conn.execute(
        """
        INSERT INTO sync_log (category, run_type, status, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (category, run_type, status, started_at.isoformat(sep=" "), started_at.isoformat(sep=" ")),
    )
    conn.commit()


def test_empty_db_is_one_missing_issue_not_one_per_category(conn):
    issues = check_sync(conn, max_age=MAX_AGE, now=NOW)
    assert len(issues) == 1
    assert (issues[0].category, issues[0].problem) == ("*", "missing")


def test_whole_job_not_running_is_one_stale_issue(conn):
    # One missed run makes every category stale at once; that's a single
    # failure and should produce a single alert line.
    last = NOW - timedelta(days=2)
    for category in expected_sync_categories():
        _insert_log(conn, category=category, run_type="incremental_sync", status="success", started_at=last)

    issues = check_sync(conn, max_age=MAX_AGE, now=NOW)
    assert len(issues) == 1
    assert (issues[0].category, issues[0].problem) == ("*", "stale")
    assert last.isoformat(sep=" ") in issues[0].detail


def test_backfill_not_running_is_one_stale_issue(conn):
    for category in ("sleep", "hrv_daily", "body_composition"):
        _insert_log(conn, category=category, run_type="backfill", status="success", started_at=NOW - timedelta(days=2))

    issues = check_backfill(conn, max_age=MAX_AGE, now=NOW)
    assert len(issues) == 1
    assert (issues[0].run_type, issues[0].category, issues[0].problem) == ("backfill", "*", "stale")


def test_no_issues_when_everything_recently_succeeded(conn):
    for category in expected_sync_categories():
        _insert_log(conn, category=category, run_type="incremental_sync", status="success", started_at=NOW - timedelta(hours=2))
    assert check_sync(conn, max_age=MAX_AGE, now=NOW) == []


def test_stale_run_is_flagged_even_if_it_succeeded(conn):
    for category in expected_sync_categories():
        started = NOW - timedelta(hours=2) if category != "sleep" else NOW - timedelta(days=3)
        _insert_log(conn, category=category, run_type="incremental_sync", status="success", started_at=started)

    issues = check_sync(conn, max_age=MAX_AGE, now=NOW)
    assert [i.category for i in issues] == ["sleep"]
    assert issues[0].problem == "stale"


def test_partial_status_is_flagged_even_though_it_ran_recently(conn):
    for category in expected_sync_categories():
        status = "success" if category != "hrv_daily" else "partial"
        _insert_log(conn, category=category, run_type="incremental_sync", status=status, started_at=NOW - timedelta(hours=2))

    issues = check_sync(conn, max_age=MAX_AGE, now=NOW)
    assert [i.category for i in issues] == ["hrv_daily"]
    assert issues[0].problem == "status:partial"


def test_only_the_most_recent_run_per_category_counts(conn):
    # An old failure followed by a fresh success shouldn't still be flagged.
    _insert_log(conn, category="sleep", run_type="incremental_sync", status="failed", started_at=NOW - timedelta(days=5))
    _insert_log(conn, category="sleep", run_type="incremental_sync", status="success", started_at=NOW - timedelta(hours=1))

    issues = check_run_type(conn, run_type="incremental_sync", categories={"sleep"}, max_age=MAX_AGE, now=NOW)
    assert not any(i.category == "sleep" for i in issues)


def test_backfill_flags_total_absence_as_a_single_issue(conn):
    issues = check_backfill(conn, max_age=MAX_AGE, now=NOW)
    assert len(issues) == 1
    assert issues[0].problem == "missing"
    assert issues[0].category == "*"


def test_completed_backfill_categories_are_not_expected_to_keep_running(conn):
    # Finished long ago, then the job was unloaded -- not a missed run.
    for category in ("sleep", "hrv_daily"):
        _insert_log(conn, category=category, run_type="backfill", status="success", started_at=NOW - timedelta(days=10))
        _insert_log(conn, category=category, run_type="backfill", status="complete", started_at=NOW - timedelta(days=9))
    # Still in progress and stalled -- should still be flagged.
    _insert_log(conn, category="body_composition", run_type="backfill", status="success", started_at=NOW - timedelta(days=9))

    issues = check_backfill(conn, max_age=MAX_AGE, now=NOW)
    assert len(issues) == 1
    assert (issues[0].category, issues[0].problem) == ("*", "stale")

    _insert_log(conn, category="body_composition", run_type="backfill", status="complete", started_at=NOW - timedelta(days=8))
    assert check_backfill(conn, max_age=MAX_AGE, now=NOW) == []


def test_backfill_only_checks_categories_it_has_ever_touched(conn):
    # activities is deliberately excluded from backfill in this project's
    # default plist -- backfill shouldn't be flagged as "missing" for a
    # category it was never configured to cover.
    _insert_log(conn, category="hrv_daily", run_type="backfill", status="success", started_at=NOW - timedelta(hours=2))

    issues = check_backfill(conn, max_age=MAX_AGE, now=NOW)
    assert issues == []


def test_backfill_batch_logs_complete_once_past_earliest_date(conn):
    from datetime import date

    from garmin_mcp.sync.cursor import set_cursor
    from garmin_mcp.sync.daily_categories import build_specs
    from garmin_mcp.sync.engine import backfill_batch

    spec = next(s for s in build_specs() if s.category == "sleep")
    set_cursor(conn, "sleep", "backward", "date", "2018-08-01")

    # Client/garmin are never touched on the already-finished path.
    assert backfill_batch(spec, None, None, conn, date(2026, 7, 14), earliest_date=date(2018, 8, 1)) is None
    status = conn.execute(
        "SELECT status FROM sync_log WHERE category = 'sleep' AND run_type = 'backfill'"
    ).fetchone()[0]
    assert status == "complete"
    assert check_backfill(conn, max_age=MAX_AGE, now=NOW) == []
