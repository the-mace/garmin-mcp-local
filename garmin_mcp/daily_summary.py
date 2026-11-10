"""Plain-text daily summary of the local cache, meant to be emailed to a bot.

Format (v1): a header line, then `[section key=value ...]` blocks of
`key=value` lines. Units are in the key names, missing values are left out,
and a section with no row at all is a single `status=no_data` line. Every
line splits on the first `=`, so a parser needs no knowledge of the fields.

The scheduled sync runs at 08:00, when today's step/calorie totals have only
just started, so the summary also carries yesterday's daily health and
activities as the finished numbers.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

FORMAT_VERSION = 1

Field = tuple[str, str, Callable[[object], object] | None]


def _local_hhmm(epoch_ms: object) -> str:
    return datetime.fromtimestamp(int(epoch_ms) / 1000).astimezone().strftime("%H:%M")


def _min(seconds: object) -> int:
    return round(float(seconds) / 60)


def _min1(seconds: object) -> float:
    return round(float(seconds) / 60, 1)


def _km(meters: object) -> float:
    return round(float(meters) / 1000, 2)


def _kg(grams: object) -> float:
    return round(float(grams) / 1000, 1)


def _r1(value: object) -> float:
    return round(float(value), 1)


def _hms(seconds: object) -> str:
    s = round(float(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


SLEEP: list[Field] = [
    ("sleep_start_utc_ms", "start_local", _local_hhmm),
    ("sleep_end_utc_ms", "end_local", _local_hhmm),
    ("deep_sleep_s", "deep_min", _min),
    ("light_sleep_s", "light_min", _min),
    ("rem_sleep_s", "rem_min", _min),
    ("awake_s", "awake_min", _min),
    ("awake_count", "awake_count", None),
    ("restless_moment_count", "restless_moments", None),
    ("overall_score", "score", None),
    ("quality_score", "quality_score", None),
    ("duration_score", "duration_score", None),
    ("recovery_score", "recovery_score", None),
    ("deep_score", "deep_score", None),
    ("rem_score", "rem_score", None),
    ("light_score", "light_score", None),
    ("restfulness_score", "restfulness_score", None),
    ("interruptions_score", "interruptions_score", None),
    ("avg_respiration", "avg_respiration_brpm", _r1),
    ("avg_sleep_stress", "avg_stress", _r1),
    ("feedback", "feedback", None),
]

HRV: list[Field] = [
    ("last_night_avg", "last_night_avg_ms", None),
    ("last_night_5min_high", "last_night_5min_high_ms", None),
    ("weekly_avg", "weekly_avg_ms", None),
    ("baseline_balanced_low", "baseline_low_ms", None),
    ("baseline_balanced_upper", "baseline_high_ms", None),
    ("status", "status", None),
]

READINESS: list[Field] = [
    ("score", "score", None),
    ("level", "level", None),
    ("feedback_short", "feedback", None),
    ("sleep_score", "sleep_score", None),
    ("recovery_time_minutes", "recovery_time_min", None),
    ("hrv_weekly_average", "hrv_weekly_avg_ms", None),
    ("sleep_score_factor_percent", "factor_sleep_pct", None),
    ("sleep_history_factor_percent", "factor_sleep_history_pct", None),
    ("recovery_time_factor_percent", "factor_recovery_time_pct", None),
    ("acwr_factor_percent", "factor_acwr_pct", None),
    ("stress_history_factor_percent", "factor_stress_history_pct", None),
    ("hrv_factor_percent", "factor_hrv_pct", None),
]

TRAINING_STATUS: list[Field] = [
    ("training_status", "status", None),
    ("fitness_trend", "fitness_trend", None),
    ("vo2max_running", "vo2max_running", None),
    ("vo2max_cycling", "vo2max_cycling", None),
    ("acute_load", "acute_load", _r1),
    ("chronic_load", "chronic_load", _r1),
    ("acwr_percent", "acwr_pct", None),
    ("acwr_status", "acwr_status", None),
    ("endurance_score", "endurance_score", None),
    ("hill_overall_score", "hill_score", None),
]

RACE_PREDICTIONS: list[Field] = [
    ("time_5k_s", "5k", _hms),
    ("time_10k_s", "10k", _hms),
    ("time_half_marathon_s", "half_marathon", _hms),
    ("time_marathon_s", "marathon", _hms),
]

BODY_COMPOSITION: list[Field] = [
    ("weight_g", "weight_kg", _kg),
    ("bmi", "bmi", _r1),
    ("body_fat_percent", "body_fat_pct", _r1),
    ("body_water_percent", "body_water_pct", _r1),
    ("muscle_mass_g", "muscle_mass_kg", _kg),
    ("bone_mass_g", "bone_mass_kg", _kg),
]

DAILY_HEALTH: list[Field] = [
    ("total_steps", "steps", None),
    ("daily_step_goal", "step_goal", None),
    ("total_distance_m", "distance_km", _km),
    ("total_calories", "total_kcal", round),
    ("active_calories", "active_kcal", round),
    ("intensity_minutes_moderate", "intensity_moderate_min", None),
    ("intensity_minutes_vigorous", "intensity_vigorous_min", None),
    ("resting_hr", "resting_hr_bpm", None),
    ("min_hr", "min_hr_bpm", None),
    ("max_hr", "max_hr_bpm", None),
    ("avg_stress", "avg_stress", None),
    ("max_stress", "max_stress", None),
    ("body_battery_charged", "body_battery_charged", None),
    ("body_battery_drained", "body_battery_drained", None),
    ("body_battery_highest", "body_battery_high", None),
    ("body_battery_lowest", "body_battery_low", None),
    ("body_battery_most_recent", "body_battery_latest", None),
    ("avg_spo2", "spo2_avg_pct", None),
    ("lowest_spo2", "spo2_low_pct", None),
    ("avg_waking_respiration", "respiration_avg_brpm", _r1),
    ("lowest_respiration", "respiration_low_brpm", _r1),
    ("highest_respiration", "respiration_high_brpm", _r1),
]

BODY_BATTERY: list[Field] = [
    ("body_battery_most_recent", "latest", None),
    ("body_battery_highest", "high", None),
    ("body_battery_lowest", "low", None),
    ("body_battery_charged", "charged", None),
    ("body_battery_drained", "drained", None),
]

ACTIVITY: list[Field] = [
    ("activity_id", "id", None),
    ("activity_type", "type", None),
    ("begin_timestamp_utc_ms", "start_local", _local_hhmm),
    ("duration_s", "duration_min", _min1),
    ("distance_m", "distance_km", _km),
    ("calories", "kcal", round),
    ("avg_hr", "avg_hr_bpm", round),
    ("max_hr", "max_hr_bpm", round),
    ("avg_power_w", "avg_power_w", round),
    ("norm_power_w", "norm_power_w", round),
    ("training_stress_score", "tss", _r1),
    ("aerobic_training_effect", "aerobic_te", _r1),
    ("anaerobic_training_effect", "anaerobic_te", _r1),
    ("activity_training_load", "training_load", _r1),
    ("elevation_gain_m", "elevation_gain_m", round),
    ("name", "name", None),
]


def _pairs(row: sqlite3.Row, fields: list[Field]) -> list[tuple[str, object]]:
    out = []
    for column, key, fmt in fields:
        value = row[column]
        if value is None or value == "":
            continue
        out.append((key, fmt(value) if fmt else value))
    return out


def _clean(value: object) -> str:
    # One record per line: a newline inside a feedback string would break parsing.
    return " ".join(str(value).split())


def _day_section(conn: sqlite3.Connection, name: str, table: str, fields: list[Field], day: date, note: str = "") -> list[str]:
    header = f"[{name} date={day.isoformat()}{note}]"
    row = conn.execute(f"SELECT * FROM {table} WHERE calendar_date = ?", (day.isoformat(),)).fetchone()
    if row is None:
        return [header, "status=no_data"]
    pairs = _pairs(row, fields)
    if not pairs:
        return [header, "status=no_data"]
    return [header, *(f"{k}={_clean(v)}" for k, v in pairs)]


def _activities_section(conn: sqlite3.Connection, day: date, *, empty_status: str | None = None) -> list[str]:
    start = datetime.combine(day, time()).astimezone()
    end = start + timedelta(days=1)
    rows = conn.execute(
        "SELECT * FROM activities WHERE begin_timestamp_utc_ms >= ? AND begin_timestamp_utc_ms < ? "
        "ORDER BY begin_timestamp_utc_ms",
        (int(start.timestamp() * 1000), int(end.timestamp() * 1000)),
    ).fetchall()
    lines = [f"[activities date={day.isoformat()} count={len(rows)}]"]
    if not rows and empty_status:
        lines.append(f"status={empty_status}")
        return lines
    for row in rows:
        lines.append("activity: " + " ".join(f"{k}={_clean(v).replace(' ', '_')}" for k, v in _pairs(row, ACTIVITY)))
    return lines


def render_daily_summary(conn: sqlite3.Connection, day: date, *, now: datetime | None = None) -> str:
    """Render the summary for `day` (sleep, HRV and readiness are keyed to the
    wake-up date, so `day`'s row is the night just ended)."""
    conn.row_factory = sqlite3.Row
    prev = day - timedelta(days=1)
    now = (now or datetime.now()).astimezone()
    generated = now.isoformat(timespec="seconds")
    today_note = " partial=true" if day >= now.date() else ""
    sections = [
        [f"garmin_daily_summary v{FORMAT_VERSION}", f"date={day.isoformat()}", f"generated={generated}"],
        _day_section(conn, "sleep", "sleep", SLEEP, day),
        _day_section(conn, "hrv", "hrv_daily", HRV, day),
        _day_section(conn, "training_readiness", "training_readiness", READINESS, day),
        _day_section(conn, "training_status", "training_status", TRAINING_STATUS, day),
        _day_section(conn, "race_predictions", "race_predictions", RACE_PREDICTIONS, day),
        _day_section(conn, "daily_health", "daily_health_metrics", DAILY_HEALTH, day, today_note),
        _activities_section(conn, day),
        _day_section(conn, "daily_health", "daily_health_metrics", DAILY_HEALTH, prev),
        _activities_section(conn, prev),
    ]
    body_comp = _day_section(conn, "body_composition", "body_composition", BODY_COMPOSITION, day)
    if body_comp[1] != "status=no_data":
        sections.append(body_comp)
    return "\n\n".join("\n".join(s) for s in sections) + "\n"


def summary_subject(day: date) -> str:
    return f"garmin daily summary {day.isoformat()}"


def send_daily_summary(conn: sqlite3.Connection, to_addr: str, day: date) -> bool:
    """Email the summary for `day`. Never raises: a summary problem must not
    turn a successful sync into a reported crash."""
    from garmin_mcp.alerting import send_email

    try:
        body = render_daily_summary(conn, day)
    except Exception as exc:  # noqa: BLE001
        print(f"Daily summary for {day} not rendered: {exc!r}", file=sys.stderr)
        return False
    return send_email(to_addr, summary_subject(day), body)


def activities_summary_subject(day: date) -> str:
    return f"garmin activities {day.isoformat()}"


def render_activities_summary(conn: sqlite3.Connection, day: date, *, now: datetime | None = None) -> str:
    """That day's workouts and body battery; no sleep, other health metrics,
    body composition, or prior days."""
    conn.row_factory = sqlite3.Row
    now = (now or datetime.now()).astimezone()
    generated = now.isoformat(timespec="seconds")
    header = [
        f"garmin_activities_summary v{FORMAT_VERSION}",
        f"date={day.isoformat()}",
        f"generated={generated}",
    ]
    today_note = " partial=true" if day >= now.date() else ""
    sections = [
        header,
        _activities_section(conn, day, empty_status="no_workouts"),
        _day_section(conn, "body_battery", "daily_health_metrics", BODY_BATTERY, day, today_note),
    ]
    return "\n\n".join("\n".join(s) for s in sections) + "\n"


def send_activities_summary(conn: sqlite3.Connection, to_addr: str, day: date) -> bool:
    """Email the evening activities + body battery summary. Never raises."""
    from garmin_mcp.alerting import send_email

    try:
        body = render_activities_summary(conn, day)
    except Exception as exc:  # noqa: BLE001
        print(f"Activities summary for {day} not rendered: {exc!r}", file=sys.stderr)
        return False
    return send_email(to_addr, activities_summary_subject(day), body)
