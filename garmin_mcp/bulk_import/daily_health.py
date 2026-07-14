"""Import daily health metrics + sleep from Garmin's bulk export.

Sources:
  DI_CONNECT/DI-Connect-Aggregator/UDSFile_*.json  (steps/HR/stress/body battery/SpO2/respiration)
  DI_CONNECT/DI-Connect-Wellness/*_sleepData.json
"""

from __future__ import annotations

import json
import zipfile
from fnmatch import fnmatch

from garmin_mcp.db.connection import upsert
from garmin_mcp.bulk_import.report import ImportReport
from garmin_mcp.bulk_import.units import iso_to_epoch_ms

UDS_GLOB = "DI_CONNECT/DI-Connect-Aggregator/UDSFile_*.json"
SLEEP_GLOB = "DI_CONNECT/DI-Connect-Wellness/*_sleepData.json"


def _import_uds_record(conn, record: dict) -> None:
    calendar_date = record["calendarDate"]

    stress_total = {}
    for entry in (record.get("allDayStress") or {}).get("aggregatorList", []):
        if entry.get("type") == "TOTAL":
            stress_total = entry
            break

    battery = record.get("bodyBattery") or {}
    battery_by_type = {
        s["bodyBatteryStatType"]: s.get("statsValue")
        for s in battery.get("bodyBatteryStatList", [])
        if s.get("bodyBatteryStatType")
    }

    row = {
        "calendar_date": calendar_date,
        "total_steps": record.get("totalSteps"),
        "daily_step_goal": record.get("dailyStepGoal"),
        "total_distance_m": record.get("totalDistanceMeters"),
        "total_calories": record.get("totalKilocalories"),
        "active_calories": record.get("activeKilocalories"),
        "bmr_calories": record.get("bmrKilocalories"),
        "floors_ascended_m": record.get("floorsAscendedInMeters"),
        "floors_descended_m": record.get("floorsDescendedInMeters"),
        "floors_ascended_goal": record.get("userFloorsAscendedGoal"),
        "intensity_minutes_moderate": record.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": record.get("vigorousIntensityMinutes"),
        "intensity_minutes_goal": record.get("userIntensityMinutesGoal"),
        "resting_hr": record.get("restingHeartRate"),
        "current_day_resting_hr": record.get("currentDayRestingHeartRate"),
        "min_hr": record.get("minHeartRate"),
        "max_hr": record.get("maxHeartRate"),
        "avg_stress": stress_total.get("averageStressLevel"),
        "max_stress": stress_total.get("maxStressLevel"),
        "stress_duration_s": stress_total.get("stressDuration"),
        "rest_stress_duration_s": stress_total.get("restDuration"),
        "activity_stress_duration_s": stress_total.get("activityDuration"),
        "low_stress_duration_s": stress_total.get("lowDuration"),
        "medium_stress_duration_s": stress_total.get("mediumDuration"),
        "high_stress_duration_s": stress_total.get("highDuration"),
        "body_battery_charged": battery.get("chargedValue"),
        "body_battery_drained": battery.get("drainedValue"),
        "body_battery_highest": battery_by_type.get("HIGHEST"),
        "body_battery_lowest": battery_by_type.get("LOWEST"),
        "body_battery_most_recent": battery_by_type.get("MOSTRECENT"),
        "avg_spo2": record.get("averageSpo2Value"),
        "latest_spo2": record.get("latestSpo2Value"),
        "lowest_spo2": record.get("lowestSpo2Value"),
        "avg_waking_respiration": (record.get("respiration") or {}).get("avgWakingRespirationValue"),
        "highest_respiration": (record.get("respiration") or {}).get("highestRespirationValue"),
        "lowest_respiration": (record.get("respiration") or {}).get("lowestRespirationValue"),
        "latest_respiration": (record.get("respiration") or {}).get("latestRespirationValue"),
        "source": "csv_export",
    }
    upsert(conn, "daily_health_metrics", row, ["calendar_date"])

    for entry in (record.get("allDayStress") or {}).get("aggregatorList", []):
        period_type = entry.get("type")
        if not period_type:
            continue
        upsert(
            conn,
            "daily_stress_periods",
            {
                "calendar_date": calendar_date,
                "period_type": period_type,
                "avg_stress_level": entry.get("averageStressLevel"),
                "max_stress_level": entry.get("maxStressLevel"),
                "stress_duration_s": entry.get("stressDuration"),
                "rest_duration_s": entry.get("restDuration"),
                "activity_duration_s": entry.get("activityDuration"),
                "low_duration_s": entry.get("lowDuration"),
                "medium_duration_s": entry.get("mediumDuration"),
                "high_duration_s": entry.get("highDuration"),
            },
            ["calendar_date", "period_type"],
        )


def import_daily_health(zf: zipfile.ZipFile, conn, report: ImportReport) -> None:
    result = report.category("daily_health_metrics")
    matched = [n for n in zf.namelist() if fnmatch(n, UDS_GLOB)]
    result.files_found = len(matched)
    if not matched:
        result.notes.append("no UDSFile (daily aggregator) files found in export")
    for name in matched:
        with zf.open(name) as f:
            records = json.load(f)
        result.records_seen += len(records)
        for record in records:
            if "calendarDate" not in record:
                result.skipped += 1
                continue
            _import_uds_record(conn, record)
            result.rows_written += 1
    conn.commit()


def _import_sleep_record(conn, record: dict) -> None:
    scores = record.get("sleepScores") or {}
    row = {
        "calendar_date": record["calendarDate"],
        "sleep_start_utc_ms": iso_to_epoch_ms(record.get("sleepStartTimestampGMT")),
        "sleep_end_utc_ms": iso_to_epoch_ms(record.get("sleepEndTimestampGMT")),
        "deep_sleep_s": record.get("deepSleepSeconds"),
        "light_sleep_s": record.get("lightSleepSeconds"),
        "rem_sleep_s": record.get("remSleepSeconds"),
        "awake_s": record.get("awakeSleepSeconds"),
        "unmeasurable_s": record.get("unmeasurableSeconds"),
        "awake_count": record.get("awakeCount"),
        "restless_moment_count": record.get("restlessMomentCount"),
        "avg_respiration": record.get("averageRespiration"),
        "avg_sleep_stress": record.get("avgSleepStress"),
        "overall_score": scores.get("overallScore"),
        "quality_score": scores.get("qualityScore"),
        "duration_score": scores.get("durationScore"),
        "recovery_score": scores.get("recoveryScore"),
        "deep_score": scores.get("deepScore"),
        "rem_score": scores.get("remScore"),
        "light_score": scores.get("lightScore"),
        "awakenings_count_score": scores.get("awakeningsCountScore"),
        "awake_time_score": scores.get("awakeTimeScore"),
        "restfulness_score": scores.get("restfulnessScore"),
        "interruptions_score": scores.get("interruptionsScore"),
        "combined_awake_score": scores.get("combinedAwakeScore"),
        "feedback": scores.get("feedback"),
        "insight": scores.get("insight"),
        "sleep_window_confirmation_type": record.get("sleepWindowConfirmationType"),
        "source": "csv_export",
    }
    upsert(conn, "sleep", row, ["calendar_date"])


def import_sleep(zf: zipfile.ZipFile, conn, report: ImportReport) -> None:
    result = report.category("sleep")
    matched = [n for n in zf.namelist() if fnmatch(n, SLEEP_GLOB)]
    result.files_found = len(matched)
    if not matched:
        result.notes.append("no sleepData files found in export")
    for name in matched:
        with zf.open(name) as f:
            records = json.load(f)
        result.records_seen += len(records)
        for record in records:
            if "calendarDate" not in record:
                result.skipped += 1
                continue
            _import_sleep_record(conn, record)
            result.rows_written += 1
    conn.commit()
