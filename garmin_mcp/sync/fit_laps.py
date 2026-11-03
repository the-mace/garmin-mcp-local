"""Turn an activity's original FIT file into `activity_laps` rows.

The official Personal API has no splits endpoint; laps come from the FIT
file instead. Rows match what `_map_live_lap` builds from Connect's
`/splits` JSON (verified lap-for-lap against stored splits for runs and
rides), so the source of a lap row doesn't change its meaning.
"""

from __future__ import annotations

import io
import json
import zipfile

from garmin_fit_sdk import Decoder, Stream

# FIT timestamps count seconds from 1989-12-31T00:00:00Z.
_FIT_EPOCH_OFFSET_S = 631065600

# FIT stores foot-sport cadence per leg (strides/min); Connect reports
# steps/min, i.e. double, with the fractional part included.
_STEP_CADENCE_SPORTS = {"running", "walking", "hiking"}


def _unzip_fit(payload: bytes) -> bytes:
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            name = next(n for n in archive.namelist() if n.lower().endswith(".fit"))
            return archive.read(name)
    return payload


def _cadence(lap: dict, which: str, double: bool) -> float | None:
    whole = lap.get(f"{which}_cadence")
    if whole is None:
        return None
    value = whole + (lap.get(f"{which}_fractional_cadence") or 0)
    return value * 2 if double else float(value)


def _utc_ms(fit_seconds: int | None) -> int | None:
    return None if fit_seconds is None else (fit_seconds + _FIT_EPOCH_OFFSET_S) * 1000


def parse_fit_laps(activity_id: int, payload: bytes) -> list[dict]:
    """`payload` is the download as served (a zip) or a bare .fit file."""
    messages, errors = Decoder(Stream.from_byte_array(bytearray(_unzip_fit(payload)))).read(
        convert_datetimes_to_dates=False
    )
    if errors:
        raise ValueError(f"FIT decode errors for activity {activity_id}: {errors}")

    session_sport = next((s.get("sport") for s in messages.get("session_mesgs", []) if s.get("sport")), None)
    rows = []
    for position, lap in enumerate(messages.get("lap_mesgs", []), start=1):
        double = (lap.get("sport") or session_sport) in _STEP_CADENCE_SPORTS
        start_ms = _utc_ms(lap.get("start_time"))
        elapsed = lap.get("total_elapsed_time")
        intensity = lap.get("intensity")
        index = lap.get("message_index")
        rows.append(
            {
                "activity_id": activity_id,
                "lap_index": index + 1 if isinstance(index, int) else position,
                "start_time_utc_ms": start_ms,
                "end_time_utc_ms": start_ms + int(elapsed * 1000) if start_ms is not None and elapsed is not None else None,
                "distance_m": lap.get("total_distance"),
                "duration_s": lap.get("total_timer_time"),
                "avg_hr": lap.get("avg_heart_rate"),
                "max_hr": lap.get("max_heart_rate"),
                "avg_power_w": lap.get("avg_power"),
                "max_power_w": lap.get("max_power"),
                "avg_speed_mps": lap.get("enhanced_avg_speed", lap.get("avg_speed")),
                "max_speed_mps": lap.get("enhanced_max_speed", lap.get("max_speed")),
                "avg_cadence": _cadence(lap, "avg", double),
                "max_cadence": _cadence(lap, "max", double),
                "elevation_gain_m": lap.get("total_ascent"),
                "elevation_loss_m": lap.get("total_descent"),
                "calories": lap.get("total_calories"),
                "intensity_type": intensity.upper() if isinstance(intensity, str) else None,
                "workout_step_index": lap.get("wkt_step_index"),
                # Unknown/developer fields decode under integer keys; drop them.
                "measurements_json": json.dumps(
                    {k: v for k, v in lap.items() if isinstance(k, str)}, default=str, sort_keys=True
                ),
            }
        )
    return rows
