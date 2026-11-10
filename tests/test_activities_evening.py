"""Evening activities-only sync and summary email."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from garmin_mcp import daily_summary
from garmin_mcp.daily_summary import (
    activities_summary_subject,
    render_activities_summary,
    send_activities_summary,
)
from garmin_mcp.db.connection import init_db, upsert
from garmin_mcp.garmin_client.factory import GarminClients
from garmin_mcp.sync.activities_sync import sync_todays_activities
from garmin_mcp.sync.runner import run_sync, run_todays_activities_sync

DAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 8, 19, 0).astimezone()


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def _local_ms(d: date, hh: int, mm: int) -> int:
    return int(datetime.combine(d, time(hh, mm)).astimezone().timestamp() * 1000)


class PassThroughLimiter:
    def call(self, category, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class FakeGarmin:
    def get_activity_splits(self, activity_id):
        return {"lapDTOs": []}


class ListPersonal:
    def __init__(self, listing):
        self.listing = listing
        self.calls = []

    def get_activities(self, start, limit):
        self.calls.append((start, limit))
        return self.listing[start : start + limit]

    def download_activity_fit(self, activity_id):
        return None


def _listing_row(activity_id: int, begin_ms: int, name: str = "Ride"):
    return {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": "cycling"},
        "beginTimestamp": begin_ms,
        "duration": 1800,
        "distance": 10000,
        "calories": 200,
    }


def test_subject_is_exact_prefix_plus_iso_date():
    assert activities_summary_subject(DAY) == "garmin activities 2026-11-08"


def test_render_activities_summary_only_todays_workouts(conn):
    for activity_id, d, hh, name in [
        (1, DAY, 7, "Morning Ride"),
        (2, DAY, 17, "Evening Ride"),
        (3, date(2026, 11, 7), 18, "Yesterday"),
        (4, date(2026, 11, 9), 6, "Tomorrow"),
    ]:
        upsert(
            conn,
            "activities",
            {
                "activity_id": activity_id,
                "activity_type": "cycling",
                "begin_timestamp_utc_ms": _local_ms(d, hh, 0),
                "duration_s": 1800,
                "name": name,
            },
            ["activity_id"],
        )
    upsert(
        conn,
        "sleep",
        {"calendar_date": DAY.isoformat(), "overall_score": 80},
        ["calendar_date"],
    )
    upsert(
        conn,
        "daily_health_metrics",
        {"calendar_date": DAY.isoformat(), "total_steps": 5000},
        ["calendar_date"],
    )
    conn.commit()

    text = render_activities_summary(conn, DAY, now=NOW)
    assert text.startswith("garmin_activities_summary v1\ndate=2026-11-08\n")
    assert "[activities date=2026-11-08 count=2]" in text
    assert "Morning_Ride" in text and "Evening_Ride" in text
    assert "Yesterday" not in text and "Tomorrow" not in text
    assert "sleep" not in text
    assert "daily_health" not in text
    assert "steps=" not in text
    assert "body_composition" not in text
    assert "hrv" not in text


def test_render_activities_summary_includes_body_battery(conn):
    upsert(
        conn,
        "daily_health_metrics",
        {
            "calendar_date": DAY.isoformat(),
            "total_steps": 5000,
            "body_battery_most_recent": 34,
            "body_battery_highest": 88,
            "body_battery_lowest": 30,
            "body_battery_charged": 55,
            "body_battery_drained": 60,
        },
        ["calendar_date"],
    )
    conn.commit()
    text = render_activities_summary(conn, DAY, now=NOW)
    section = text.split("[body_battery date=2026-11-08 partial=true]\n", 1)[1]
    assert section == "latest=34\nhigh=88\nlow=30\ncharged=55\ndrained=60\n"


def test_body_battery_no_data(conn):
    text = render_activities_summary(conn, DAY, now=NOW)
    assert text.endswith("[body_battery date=2026-11-08 partial=true]\nstatus=no_data\n")


def test_no_workouts_status_line(conn):
    text = render_activities_summary(conn, DAY, now=NOW)
    assert "[activities date=2026-11-08 count=0]" in text
    assert "status=no_workouts" in text
    assert "sleep" not in text


def test_send_activities_summary_subject_and_recipient(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("garmin_mcp.alerting.send_email", lambda *a: sent.append(a) or True)
    assert send_activities_summary(conn, "bot@example.com", DAY)
    to, subject, body = sent[0]
    assert to == "bot@example.com"
    assert subject == "garmin activities 2026-11-08"
    assert "garmin_activities_summary v1" in body
    assert "status=no_workouts" in body


def test_send_never_raises_on_render_error(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("garmin_mcp.alerting.send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(daily_summary, "render_activities_summary", lambda *a, **k: 1 / 0)
    assert send_activities_summary(conn, "bot@example.com", DAY) is False
    assert sent == []


def test_sync_todays_activities_filters_and_stops_before_day(conn):
    today_ms = _local_ms(DAY, 17, 0)
    morning_ms = _local_ms(DAY, 7, 0)
    yesterday_ms = _local_ms(date(2026, 11, 7), 18, 0)
    listing = [
        _listing_row(20, today_ms, "Evening"),
        _listing_row(19, morning_ms, "Morning"),
        _listing_row(18, yesterday_ms, "Yesterday"),
        _listing_row(17, yesterday_ms - 1000, "Older"),
    ]
    personal = ListPersonal(listing)

    result = sync_todays_activities(
        PassThroughLimiter(),
        GarminClients(personal, FakeGarmin),
        conn,
        day=DAY,
        page_size=2,
    )

    ids = {r[0] for r in conn.execute("SELECT activity_id FROM activities")}
    assert ids == {19, 20}
    assert result.activities_written == 2
    assert result.reached_before_day is True
    assert result.pages_fetched == 2  # page0 today rows; page1 hits yesterday and stops
    log = conn.execute(
        "SELECT run_type, range_start, status FROM sync_log WHERE category = 'activities'"
    ).fetchone()
    assert tuple(log) == ("evening_activities", "2026-11-08", "success")


def test_sync_todays_activities_does_not_write_other_days(conn):
    yesterday_ms = _local_ms(date(2026, 11, 7), 12, 0)
    listing = [_listing_row(1, yesterday_ms, "Only Yesterday")]
    result = sync_todays_activities(
        PassThroughLimiter(),
        GarminClients(ListPersonal(listing), FakeGarmin),
        conn,
        day=DAY,
        page_size=10,
    )
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert result.activities_written == 0
    assert result.reached_before_day is True


def test_runner_evening_syncs_activities_and_todays_health_only(conn, monkeypatch):
    calls = []

    def fake_sync(*a, **k):
        calls.append("evening")
        return "ok"

    def fake_daily(spec, rlc, clients, c, dates, run_type):
        calls.append((spec.category, dates, run_type))
        return "h"

    monkeypatch.setattr("garmin_mcp.sync.runner.sync_todays_activities", fake_sync)
    monkeypatch.setattr("garmin_mcp.sync.runner.sync_daily_category", fake_daily)
    monkeypatch.setattr(
        "garmin_mcp.sync.runner.incremental_sync_activities",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("full activities sync")),
    )
    monkeypatch.setattr(
        "garmin_mcp.sync.runner.incremental_sync",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("daily category sync")),
    )

    out = run_todays_activities_sync(conn, object(), object(), today=DAY)
    assert out == {"activities": "ok", "daily_health_metrics": "h"}
    assert calls == ["evening", ("daily_health_metrics", ["2026-11-08"], "evening_body_battery")]


def test_evening_health_fetch_leaves_forward_cursor_alone(conn):
    from garmin_mcp.sync.cursor import get_cursor

    class Personal(ListPersonal):
        def get_user_summary(self, cdate):
            return {"bodyBatteryMostRecentValue": 41, "bodyBatteryHighestValue": 90}

    clients = GarminClients(Personal([]), FakeGarmin())
    results = run_todays_activities_sync(conn, clients, PassThroughLimiter(), today=DAY)

    assert results["daily_health_metrics"].succeeded == ["2026-11-08"]
    row = conn.execute(
        "SELECT body_battery_most_recent, body_battery_highest FROM daily_health_metrics WHERE calendar_date = ?",
        (DAY.isoformat(),),
    ).fetchone()
    assert tuple(row) == (41, 90)
    assert get_cursor(conn, "daily_health_metrics", "forward") is None
    run_types = {r[0] for r in conn.execute("SELECT run_type FROM sync_log")}
    assert "incremental_sync" not in run_types


def test_full_run_sync_still_covers_categories(conn, monkeypatch):
    seen = []

    monkeypatch.setattr(
        "garmin_mcp.sync.runner.incremental_sync_activities",
        lambda *a, **k: seen.append("activities") or "a",
    )
    monkeypatch.setattr(
        "garmin_mcp.sync.runner.build_specs",
        lambda: [type("S", (), {"category": "sleep"})()],
    )
    monkeypatch.setattr(
        "garmin_mcp.sync.runner.incremental_sync",
        lambda *a, **k: seen.append("sleep") or "s",
    )
    out = run_sync(conn, object(), object(), today=DAY)
    assert out == {"activities": "a", "sleep": "s"}
    assert seen == ["activities", "sleep"]
