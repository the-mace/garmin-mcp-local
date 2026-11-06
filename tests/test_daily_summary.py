from __future__ import annotations

from datetime import date, datetime, time

import pytest

from garmin_mcp import daily_summary
from garmin_mcp.daily_summary import render_daily_summary, send_daily_summary
from garmin_mcp.db.connection import init_db, upsert

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 6, 5).astimezone()


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def _sections(text: str) -> dict[str, dict[str, str]]:
    """Parse the way a bot would: header blocks, then key=value lines."""
    out: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("["):
            current = line.strip("[]")
            out[current] = {}
        elif current and "=" in line and not line.startswith("activity:"):
            key, value = line.split("=", 1)
            out[current][key] = value
    return out


def _local_ms(d: date, hh: int, mm: int) -> int:
    return int(datetime.combine(d, time(hh, mm)).astimezone().timestamp() * 1000)


def test_sections_and_units(conn):
    upsert(conn, "sleep", {
        "calendar_date": DAY.isoformat(),
        "sleep_start_utc_ms": _local_ms(date(2026, 3, 9), 23, 30),
        "deep_sleep_s": 3600,
        "rem_sleep_s": 5430,
        "overall_score": 81,
        "feedback": "LINE\nBREAK",
    }, ["calendar_date"])
    upsert(conn, "race_predictions", {"calendar_date": DAY.isoformat(), "time_5k_s": 1927.4}, ["calendar_date"])
    upsert(conn, "daily_health_metrics", {"calendar_date": "2026-03-09", "total_steps": 9000, "total_distance_m": 7123}, ["calendar_date"])
    conn.commit()

    s = _sections(render_daily_summary(conn, DAY, now=NOW))

    sleep = s["sleep date=2026-03-10"]
    assert sleep["start_local"] == "23:30"
    assert sleep["deep_min"] == "60"
    assert sleep["rem_min"] == "90"
    assert sleep["feedback"] == "LINE BREAK"
    assert "light_min" not in sleep
    assert s["race_predictions date=2026-03-10"]["5k"] == "0:32:07"
    assert s["daily_health date=2026-03-09"] == {"steps": "9000", "distance_km": "7.12"}
    assert s["daily_health date=2026-03-10 partial=true"] == {"status": "no_data"}
    assert s["hrv date=2026-03-10"] == {"status": "no_data"}


def test_past_day_is_not_marked_partial(conn):
    text = render_daily_summary(conn, date(2026, 3, 1), now=NOW)
    assert "[daily_health date=2026-03-01]" in text


def test_body_composition_only_when_weighed_that_day(conn):
    assert "body_composition" not in render_daily_summary(conn, DAY, now=NOW)
    upsert(conn, "body_composition", {"calendar_date": DAY.isoformat(), "weight_g": 80250}, ["calendar_date"])
    conn.commit()
    assert _sections(render_daily_summary(conn, DAY, now=NOW))["body_composition date=2026-03-10"] == {"weight_kg": "80.2"}


def test_activities_bucketed_by_local_day(conn):
    for activity_id, d, hh in [(1, DAY, 0), (2, DAY, 23), (3, date(2026, 3, 11), 0), (4, date(2026, 3, 9), 18)]:
        upsert(conn, "activities", {
            "activity_id": activity_id,
            "activity_type": "indoor_cycling",
            "begin_timestamp_utc_ms": _local_ms(d, hh, 5),
            "duration_s": 1800,
            "name": "Power Zone Ride",
        }, ["activity_id"])
    conn.commit()

    lines = render_daily_summary(conn, DAY, now=NOW).splitlines()
    i = lines.index("[activities date=2026-03-10 count=2]")
    assert lines[i + 1] == "activity: id=1 type=indoor_cycling start_local=00:05 duration_min=30.0 name=Power_Zone_Ride"
    assert lines[i + 2].startswith("activity: id=2 ")
    assert "[activities date=2026-03-09 count=1]" in lines


def test_send_never_raises_on_render_error(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("garmin_mcp.alerting.send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(daily_summary, "render_daily_summary", lambda *a, **k: 1 / 0)
    assert send_daily_summary(conn, "bot@example.com", DAY) is False
    assert sent == []


def test_send_uses_subject_with_date(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("garmin_mcp.alerting.send_email", lambda *a: sent.append(a) or True)
    assert send_daily_summary(conn, "bot@example.com", DAY)
    to, subject, body = sent[0]
    assert (to, subject) == ("bot@example.com", "garmin daily summary 2026-03-10")
    assert body.startswith("garmin_daily_summary v1\ndate=2026-03-10\n")
