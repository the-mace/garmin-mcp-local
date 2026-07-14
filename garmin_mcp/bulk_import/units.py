"""Unit conversions for Garmin's bulk export.

Garmin's raw internal units (verified empirically against a real export by
cross-checking against known real-world values -- e.g. a road ride's raw
`distance` field only made sense as kilometers once divided by 100 to get
centimeters -> meters, cross-checked against the ride's actual known
distance):

  * distance-like fields (distance, elevationGain/Loss, min/maxElevation,
    avgStrideLength, avgStrokeDistance) are in **centimeters**.
  * speed-like fields (avgSpeed, maxSpeed, avgVerticalSpeed,
    maxVerticalSpeed, avgGradeAdjustedSpeed) are in
    **centimeters per millisecond** (1 cm/ms = 10 m/s).
  * duration-like fields (duration, elapsedDuration, movingDuration) are
    in **milliseconds**.
  * lat/long, calories, HR (bpm), power (watts) are already in normal
    human units -- no conversion.

Running-dynamics fields (avgVerticalOscillation, avgGroundContactTime,
avgVerticalRatio, cadence) are left as raw values from the export: their
exact unit wasn't independently verifiable from this account's sample
data (no confirmed ground-truth to cross-check against), so converting
them would risk *introducing* a wrong number rather than preserving an
ambiguous one. See README for details.
"""

from __future__ import annotations

from datetime import datetime, timezone


def cm_to_m(value: float | None) -> float | None:
    return None if value is None else value / 100.0


def cm_per_ms_to_mps(value: float | None) -> float | None:
    return None if value is None else value * 10.0


def ms_to_s(value: float | None) -> float | None:
    return None if value is None else value / 1000.0


def epoch_ms_to_date(value: int | float | None) -> str | None:
    """Convert an epoch-milliseconds timestamp to a 'YYYY-MM-DD' calendar
    date string, as used by the EnduranceScore/HillScore/AcuteTrainingLoad
    export files (unlike most other files, their `calendarDate` field is
    epoch ms rather than an ISO date string).
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def iso_to_epoch_ms(value: str | None) -> int | None:
    """Convert one of Garmin's '...GMT' ISO-ish timestamp strings (e.g.
    '2025-12-27T03:37:40.0', naive, but GMT/UTC per the field name) to
    epoch milliseconds, so timestamp columns stay a consistent INTEGER
    type regardless of which export file they came from (some fields in
    the same file are epoch ms already, others are ISO strings).
    """
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
