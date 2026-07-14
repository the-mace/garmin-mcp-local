"""Import activities (+ laps + HR/power zones) from Garmin's bulk export.

Source: DI_CONNECT/DI-Connect-Fitness/*_summarizedActivities.json
Each file is `[{"summarizedActivitiesExport": [ {...activity...}, ... ]}]`.
"""

from __future__ import annotations

import json
import zipfile
from fnmatch import fnmatch

from garmin_mcp.db.connection import upsert
from garmin_mcp.bulk_import.report import ImportReport
from garmin_mcp.bulk_import.units import cm_per_ms_to_mps, cm_to_m, ms_to_s

ACTIVITIES_GLOB = "DI_CONNECT/DI-Connect-Fitness/*_summarizedActivities.json"

# candidate fieldEnum names per target lap column, in priority order --
# Garmin's own export is inconsistent about e.g. MAX_BIKE_CADENCE vs
# MIN_BIKECADENCE (underscore placement varies), so we try several.
LAP_FIELD_CANDIDATES = {
    "avg_hr": ["WEIGHTED_MEAN_HEARTRATE"],
    "max_hr": ["MAX_HEARTRATE"],
    "avg_power_w": ["WEIGHTED_MEAN_POWER"],
    "max_power_w": ["MAX_POWER"],
    "avg_speed_mps": ["WEIGHTED_MEAN_SPEED", "WEIGHTED_MEAN_MOVINGSPEED"],
    "max_speed_mps": ["MAX_SPEED"],
    "avg_cadence": ["WEIGHTED_MEAN_BIKECADENCE", "WEIGHTED_MEAN_RUNCADENCE", "WEIGHTED_MEAN_CADENCE"],
    "max_cadence": ["MAX_BIKE_CADENCE", "MAX_BIKECADENCE", "MAX_RUN_CADENCE", "MAX_RUNCADENCE", "MAX_CADENCE"],
    "distance_m": ["SUM_DISTANCE"],
    "duration_s": ["SUM_DURATION"],
    "elevation_gain_m": ["SUM_ELEVATIONGAIN"],
    "elevation_loss_m": ["SUM_ELEVATIONLOSS"],
    "calories": ["SUM_KILOCALORIES", "SUM_CALORIES"],
}
LAP_FIELD_CM = {"distance_m", "elevation_gain_m", "elevation_loss_m"}
LAP_FIELD_CM_PER_MS = {"avg_speed_mps", "max_speed_mps"}
LAP_FIELD_MS = {"duration_s"}


def _extract_lap_fields(measurements: list[dict]) -> dict:
    by_field = {m["fieldEnum"]: m.get("value") for m in measurements if m.get("fieldEnum")}
    out = {}
    for column, candidates in LAP_FIELD_CANDIDATES.items():
        value = None
        for candidate in candidates:
            if candidate in by_field and by_field[candidate] is not None:
                value = by_field[candidate]
                break
        if value is None:
            out[column] = None
            continue
        if column in LAP_FIELD_CM:
            value = cm_to_m(value)
        elif column in LAP_FIELD_CM_PER_MS:
            value = cm_per_ms_to_mps(value)
        elif column in LAP_FIELD_MS:
            value = ms_to_s(value)
        out[column] = value
    return out


def _import_one_activity(conn, act: dict) -> None:
    activity_id = act["activityId"]

    row = {
        "activity_id": activity_id,
        "uuid": f"{act.get('uuidMsb')}:{act.get('uuidLsb')}" if act.get("uuidMsb") is not None else None,
        "name": act.get("name"),
        "activity_type": act.get("activityType"),
        "sport_type": act.get("sportType"),
        "event_type_id": act.get("eventTypeId"),
        "begin_timestamp_utc_ms": int(act["beginTimestamp"]),
        "begin_timestamp_local_ms": int(act["startTimeLocal"]) if act.get("startTimeLocal") is not None else None,
        "timezone_id": act.get("timeZoneId"),
        "duration_s": ms_to_s(act.get("duration")),
        "elapsed_duration_s": ms_to_s(act.get("elapsedDuration")),
        "moving_duration_s": ms_to_s(act.get("movingDuration")),
        "distance_m": cm_to_m(act.get("distance")),
        "calories": act.get("calories"),
        "avg_hr": act.get("avgHr"),
        "max_hr": act.get("maxHr"),
        "min_hr": act.get("minHr"),
        "avg_speed_mps": cm_per_ms_to_mps(act.get("avgSpeed")),
        "max_speed_mps": cm_per_ms_to_mps(act.get("maxSpeed")),
        "elevation_gain_m": cm_to_m(act.get("elevationGain")),
        "elevation_loss_m": cm_to_m(act.get("elevationLoss")),
        "min_elevation_m": cm_to_m(act.get("minElevation")),
        "max_elevation_m": cm_to_m(act.get("maxElevation")),
        "avg_power_w": act.get("avgPower"),
        "max_power_w": act.get("maxPower"),
        "norm_power_w": act.get("normPower"),
        "max_20min_power_w": act.get("max20MinPower"),
        "avg_bike_cadence": act.get("avgBikeCadence"),
        "max_bike_cadence": act.get("maxBikeCadence"),
        "avg_run_cadence": act.get("avgRunCadence"),
        "max_run_cadence": act.get("maxRunCadence"),
        "avg_double_cadence": act.get("avgDoubleCadence"),
        "max_double_cadence": act.get("maxDoubleCadence"),
        "avg_swim_cadence": act.get("avgSwimCadence"),
        "avg_stroke_distance": cm_to_m(act.get("avgStrokeDistance")),
        "avg_strokes": act.get("avgStrokes"),
        "avg_swolf": act.get("avgSwolf"),
        "strokes": act.get("strokes"),
        "pool_length": act.get("poolLength"),
        "steps": act.get("steps"),
        "total_reps": act.get("totalReps"),
        "total_sets": act.get("totalSets"),
        "training_stress_score": act.get("trainingStressScore"),
        "intensity_factor": act.get("intensityFactor"),
        "aerobic_training_effect": act.get("aerobicTrainingEffect"),
        "aerobic_training_effect_message": act.get("aerobicTrainingEffectMessage"),
        "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
        "anaerobic_training_effect_message": act.get("anaerobicTrainingEffectMessage"),
        "training_effect_label": act.get("trainingEffectLabel"),
        "activity_training_load": act.get("activityTrainingLoad"),
        "vo2max_value": act.get("vO2MaxValue"),
        "avg_respiration_rate": act.get("avgRespirationRate"),
        "max_respiration_rate": act.get("maxRespirationRate"),
        "min_respiration_rate": act.get("minRespirationRate"),
        "avg_stress": act.get("avgStress"),
        "start_stress": act.get("startStress"),
        "end_stress": act.get("endStress"),
        "difference_stress": act.get("differenceStress"),
        "difference_body_battery": act.get("differenceBodyBattery"),
        "avg_vertical_oscillation": act.get("avgVerticalOscillation"),
        "avg_vertical_ratio": act.get("avgVerticalRatio"),
        "avg_ground_contact_time": act.get("avgGroundContactTime"),
        "avg_vertical_speed": cm_per_ms_to_mps(act.get("avgVerticalSpeed")),
        "max_vertical_speed": cm_per_ms_to_mps(act.get("maxVerticalSpeed")),
        "avg_grade_adjusted_speed": cm_per_ms_to_mps(act.get("avgGradeAdjustedSpeed")),
        "device_id": act.get("deviceId"),
        "manufacturer": act.get("manufacturer"),
        "course_id": act.get("courseId"),
        "workout_id": act.get("workoutId"),
        "workout_feel": act.get("workoutFeel"),
        "workout_rpe": act.get("workoutRpe"),
        "location_name": act.get("locationName"),
        "description": act.get("description"),
        "start_latitude": act.get("startLatitude"),
        "start_longitude": act.get("startLongitude"),
        "end_latitude": act.get("endLatitude"),
        "end_longitude": act.get("endLongitude"),
        "min_latitude": act.get("minLatitude"),
        "min_longitude": act.get("minLongitude"),
        "max_latitude": act.get("maxLatitude"),
        "max_longitude": act.get("maxLongitude"),
        "lap_count": act.get("lapCount"),
        "favorite": int(bool(act.get("favorite"))) if act.get("favorite") is not None else None,
        "pr": int(bool(act.get("pr"))) if act.get("pr") is not None else None,
        "purposeful": int(bool(act.get("purposeful"))) if act.get("purposeful") is not None else None,
        "elevation_corrected": int(bool(act.get("elevationCorrected"))) if act.get("elevationCorrected") is not None else None,
        "water_estimated": act.get("waterEstimated"),
        "max_temperature": act.get("maxTemperature"),
        "min_temperature": act.get("minTemperature"),
        "moderate_intensity_minutes": act.get("moderateIntensityMinutes"),
        "vigorous_intensity_minutes": act.get("vigorousIntensityMinutes"),
        "exercise_sets_json": json.dumps(act["summarizedExerciseSets"]) if act.get("summarizedExerciseSets") else None,
        "dive_info_json": json.dumps(act["summarizedDiveInfo"]) if act.get("summarizedDiveInfo") else None,
        "split_summaries_json": json.dumps(act["splitSummaries"]) if act.get("splitSummaries") else None,
        "source": "csv_export",
    }
    upsert(conn, "activities", row, ["activity_id"])

    for zone_number in range(8):
        seconds = act.get(f"hrTimeInZone_{zone_number}")
        if seconds is not None:
            upsert(
                conn,
                "activity_hr_zones",
                {"activity_id": activity_id, "zone_number": zone_number, "seconds_in_zone": seconds},
                ["activity_id", "zone_number"],
            )
        seconds = act.get(f"powerTimeInZone_{zone_number}")
        if seconds is not None:
            upsert(
                conn,
                "activity_power_zones",
                {"activity_id": activity_id, "zone_number": zone_number, "seconds_in_zone": seconds},
                ["activity_id", "zone_number"],
            )

    for lap_index, split in enumerate(act.get("splits") or [], start=1):
        measurements = split.get("measurements") or []
        lap_row = {
            "activity_id": activity_id,
            "lap_index": lap_index,
            "start_time_utc_ms": split.get("startTimeGMT"),
            "end_time_utc_ms": split.get("endTimeGMT"),
            "measurements_json": json.dumps(measurements),
            **_extract_lap_fields(measurements),
        }
        upsert(conn, "activity_laps", lap_row, ["activity_id", "lap_index"])


def import_activities(zf: zipfile.ZipFile, conn, report: ImportReport) -> None:
    result = report.category("activities")
    matched = [n for n in zf.namelist() if fnmatch(n, ACTIVITIES_GLOB)]
    result.files_found = len(matched)
    if not matched:
        result.notes.append("no summarizedActivities file found in export")
        return

    for name in matched:
        with zf.open(name) as f:
            data = json.load(f)
        for wrapper in data:
            activities = wrapper.get("summarizedActivitiesExport", [])
            result.records_seen += len(activities)
            for act in activities:
                if "activityId" not in act:
                    result.skipped += 1
                    continue
                _import_one_activity(conn, act)
                result.rows_written += 1
    conn.commit()
