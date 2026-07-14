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


def test_every_expected_category_missing_on_an_empty_db(conn):
    issues = check_sync(conn, max_age=MAX_AGE, now=NOW)
    flagged = {i.category for i in issues}
    assert flagged == expected_sync_categories()
    assert all(i.problem == "missing" for i in issues)


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


def test_backfill_only_checks_categories_it_has_ever_touched(conn):
    # activities is deliberately excluded from backfill in this project's
    # default plist -- backfill shouldn't be flagged as "missing" for a
    # category it was never configured to cover.
    _insert_log(conn, category="hrv_daily", run_type="backfill", status="success", started_at=NOW - timedelta(hours=2))

    issues = check_backfill(conn, max_age=MAX_AGE, now=NOW)
    assert issues == []
