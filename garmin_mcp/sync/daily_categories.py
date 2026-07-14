"""Per-category fetch + row-mapping definitions for the daily-cadence sync
engine (garmin_mcp.sync.engine).

CAVEAT: this project was built and verified against a real Garmin bulk
export archive, but these live-API field mappings could not be verified
against a real Garmin Connect account in this environment (no live
credentials available here). Garmin Connect's live JSON endpoints are
widely believed to share field names with the bulk export (same backend),
and the mappings below follow that assumption plus this library's public
docstrings, but you should sanity-check the first `garmin-mcp-sync` run
(e.g. `SELECT * FROM daily_health_metrics ORDER BY calendar_date DESC LIMIT 3`)
and adjust field names here if anything comes back NULL that shouldn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from garminconnect import Garmin


@dataclass
class DailyCategorySpec:
    category: str  # sync_log category name
    table: str  # target table, keyed by calendar_date
    fetch: Callable[[Garmin, str], Any]
    map_row: Callable[[str, Any], dict | None]
    extra: Callable[[Any, str, Any], None] | None = None  # (conn, cdate, raw) -> None, for child tables


def _map_daily_health(cdate: str, raw: dict) -> dict | None:
    if not raw:
        return None
    # get_user_summary's shape is completely flat -- unlike the bulk
    # export's UDS aggregator, stress/body battery/respiration are NOT
    # nested sub-objects here, and several field names differ (verified
    # against a real account; see _extra_stress_periods for the one field
    # that's genuinely unavailable from this endpoint).
    return {
        "calendar_date": cdate,
        "total_steps": raw.get("totalSteps"),
        "daily_step_goal": raw.get("dailyStepGoal"),
        "total_distance_m": raw.get("totalDistanceMeters"),
        "total_calories": raw.get("totalKilocalories"),
        "active_calories": raw.get("activeKilocalories"),
        "bmr_calories": raw.get("bmrKilocalories"),
        "floors_ascended_m": raw.get("floorsAscendedInMeters"),
        "floors_descended_m": raw.get("floorsDescendedInMeters"),
        "floors_ascended_goal": raw.get("userFloorsAscendedGoal"),
        "intensity_minutes_moderate": raw.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": raw.get("vigorousIntensityMinutes"),
        "intensity_minutes_goal": raw.get("intensityMinutesGoal"),
        # Live only exposes one resting-HR reading for the day (matches the
        # bulk export's currentDayRestingHeartRate, not its separate,
        # longer-smoothed restingHeartRate baseline) -- both columns get the
        # same value from live sync since there's no equivalent second field.
        "resting_hr": raw.get("restingHeartRate"),
        "current_day_resting_hr": raw.get("restingHeartRate"),
        "min_hr": raw.get("minHeartRate"),
        "max_hr": raw.get("maxHeartRate"),
        "avg_stress": raw.get("averageStressLevel"),
        "max_stress": raw.get("maxStressLevel"),
        "stress_duration_s": raw.get("stressDuration"),
        "rest_stress_duration_s": raw.get("restStressDuration"),
        "activity_stress_duration_s": raw.get("activityStressDuration"),
        "low_stress_duration_s": raw.get("lowStressDuration"),
        "medium_stress_duration_s": raw.get("mediumStressDuration"),
        "high_stress_duration_s": raw.get("highStressDuration"),
        "body_battery_charged": raw.get("bodyBatteryChargedValue"),
        "body_battery_drained": raw.get("bodyBatteryDrainedValue"),
        "body_battery_highest": raw.get("bodyBatteryHighestValue"),
        "body_battery_lowest": raw.get("bodyBatteryLowestValue"),
        "body_battery_most_recent": raw.get("bodyBatteryMostRecentValue"),
        "avg_spo2": raw.get("averageSpo2"),
        "latest_spo2": raw.get("latestSpo2"),
        "lowest_spo2": raw.get("lowestSpo2"),
        "avg_waking_respiration": raw.get("avgWakingRespirationValue"),
        "highest_respiration": raw.get("highestRespirationValue"),
        "lowest_respiration": raw.get("lowestRespirationValue"),
        "latest_respiration": raw.get("latestRespirationValue"),
        "source": "api",
    }


def _extra_stress_periods(conn, cdate: str, raw: dict) -> None:
    # The bulk export's AWAKE/ASLEEP stress breakdown (daily_stress_periods)
    # comes from a nested `allDayStress.aggregatorList` structure that
    # get_user_summary simply doesn't return -- only a TOTAL-equivalent set
    # of flat fields is available live. Write just the TOTAL row from those;
    # AWAKE/ASLEEP rows stay CSV-only unless a future addition wires up the
    # separate get_all_day_stress() endpoint.
    from garmin_mcp.db.connection import upsert

    if raw.get("stressDuration") is None and raw.get("averageStressLevel") is None:
        return
    upsert(
        conn,
        "daily_stress_periods",
        {
            "calendar_date": cdate,
            "period_type": "TOTAL",
            "avg_stress_level": raw.get("averageStressLevel"),
            "max_stress_level": raw.get("maxStressLevel"),
            "stress_duration_s": raw.get("stressDuration"),
            "rest_duration_s": raw.get("restStressDuration"),
            "activity_duration_s": raw.get("activityStressDuration"),
            "low_duration_s": raw.get("lowStressDuration"),
            "medium_duration_s": raw.get("mediumStressDuration"),
            "high_duration_s": raw.get("highStressDuration"),
        },
        ["calendar_date", "period_type"],
    )


def _map_sleep(cdate: str, raw: dict) -> dict | None:
    if not raw:
        return None
    daily_sleep = raw.get("dailySleepDTO") or raw
    scores = daily_sleep.get("sleepScores") or {}
    if not daily_sleep.get("calendarDate") and not daily_sleep.get("sleepStartTimestampGMT"):
        return None
    from garmin_mcp.bulk_import.units import iso_to_epoch_ms

    sleep_start = daily_sleep.get("sleepStartTimestampGMT")
    sleep_end = daily_sleep.get("sleepEndTimestampGMT")
    return {
        "calendar_date": cdate,
        "sleep_start_utc_ms": sleep_start if isinstance(sleep_start, int) else iso_to_epoch_ms(sleep_start),
        "sleep_end_utc_ms": sleep_end if isinstance(sleep_end, int) else iso_to_epoch_ms(sleep_end),
        "deep_sleep_s": daily_sleep.get("deepSleepSeconds"),
        "light_sleep_s": daily_sleep.get("lightSleepSeconds"),
        "rem_sleep_s": daily_sleep.get("remSleepSeconds"),
        "awake_s": daily_sleep.get("awakeSleepSeconds"),
        "unmeasurable_s": daily_sleep.get("unmeasurableSleepSeconds"),
        "awake_count": daily_sleep.get("awakeCount"),
        "restless_moment_count": daily_sleep.get("restlessMomentCount"),
        "avg_respiration": daily_sleep.get("averageRespirationValue"),
        "avg_sleep_stress": daily_sleep.get("avgSleepStress"),
        # The live sleepScores shape only exposes an overall 0-100 score plus
        # a few qualitative buckets (qualifierKey) and raw stage percentages
        # (remPercentage/lightPercentage/deepPercentage) -- it does NOT
        # expose the per-stage 0-100 quality subscores (deepScore/remScore/
        # etc.) that the bulk export provides. Rather than fake those from a
        # differently-scaled percentage, they're left NULL for source='api'
        # rows; only overall_score is populated from live sync.
        "overall_score": (scores.get("overall") or {}).get("value"),
        "quality_score": None,
        "duration_score": None,
        "recovery_score": None,
        "deep_score": None,
        "rem_score": None,
        "light_score": None,
        "awakenings_count_score": None,
        "awake_time_score": None,
        "restfulness_score": None,
        "interruptions_score": None,
        "combined_awake_score": None,
        "feedback": daily_sleep.get("sleepScoreFeedback"),
        "insight": daily_sleep.get("sleepScoreInsight"),
        "sleep_window_confirmation_type": daily_sleep.get("sleepWindowConfirmationType"),
        "source": "api",
    }


def _map_hrv(cdate: str, raw: dict) -> dict | None:
    if not raw:
        return None
    summary = raw.get("hrvSummary") or raw
    baseline = summary.get("baseline") or {}
    return {
        "calendar_date": cdate,
        "weekly_avg": summary.get("weeklyAvg"),
        "last_night_avg": summary.get("lastNightAvg"),
        "last_night_5min_high": summary.get("lastNight5MinHigh"),
        "baseline_balanced_low": baseline.get("balancedLow"),
        "baseline_balanced_upper": baseline.get("balancedUpper"),
        "status": summary.get("status"),
        "source": "api",
    }


def _map_training_readiness(cdate: str, raw: Any) -> dict | None:
    record = raw[0] if isinstance(raw, list) and raw else raw
    if not record:
        return None
    return {
        "calendar_date": cdate,
        "score": record.get("score"),
        "level": record.get("level"),
        "feedback_long": record.get("feedbackLong"),
        "feedback_short": record.get("feedbackShort"),
        "sleep_score": record.get("sleepScore"),
        "sleep_score_factor_percent": record.get("sleepScoreFactorPercent"),
        "recovery_time_minutes": record.get("recoveryTime"),
        "recovery_time_factor_percent": record.get("recoveryTimeFactorPercent"),
        "acwr_factor_percent": record.get("acwrFactorPercent"),
        "stress_history_factor_percent": record.get("stressHistoryFactorPercent"),
        "hrv_factor_percent": record.get("hrvFactorPercent"),
        "hrv_weekly_average": record.get("hrvWeeklyAverage"),
        "sleep_history_factor_percent": record.get("sleepHistoryFactorPercent"),
        "source": "api",
    }


def _map_training_status(cdate: str, raw: dict) -> dict | None:
    if not raw:
        return None
    most_recent = raw.get("mostRecentTrainingStatus") or {}
    latest_data = most_recent.get("latestTrainingStatusData") or {}
    # keyed by deviceId (string); take whichever device reported most recently
    status_entry = next(iter(latest_data.values()), {}) if latest_data else {}
    vo2max = raw.get("mostRecentVO2Max") or {}
    return {
        "calendar_date": cdate,
        # Live `trainingStatus` is a numeric enum code (e.g. 7) with no
        # public mapping to the plain-English string the bulk export gives
        # (e.g. "MAINTAINING") -- trainingStatusFeedbackPhrase is the
        # closest human-readable equivalent available live (e.g.
        # "PRODUCTIVE_6"). Expect this column's *values* to look different
        # between source='csv_export' and source='api' rows for that reason.
        "training_status": status_entry.get("trainingStatusFeedbackPhrase"),
        # `fitnessTrendSport` is which sport the trend is computed from
        # (e.g. "CYCLING"), NOT a trend direction -- do not use it here
        # (verified against a real account; it was silently wrong before).
        # `fitnessTrend` is the only trend-direction field live exposes, and
        # it's a numeric code with no public string mapping (unlike the
        # bulk export's plain "DECREASING"/"MAINTAINING"/"NO_RESULT"), so
        # it's stored as a string of that code -- expect a different
        # *representation* between source='csv_export' and source='api'.
        "fitness_trend": str(status_entry["fitnessTrend"]) if status_entry.get("fitnessTrend") is not None else None,
        "vo2max_running": (vo2max.get("generic") or {}).get("vo2MaxPreciseValue"),
        "vo2max_cycling": (vo2max.get("cycling") or {}).get("vo2MaxPreciseValue"),
        "source": "api",
    }


def _fetch_race_predictions(garmin: Garmin, cdate: str) -> dict | None:
    # get_race_predictions() only accepts either zero args (today's latest)
    # or all three of (startdate, enddate, _type) -- a bare `cdate` isn't a
    # valid call. Request the single-day range so this fits the per-date
    # daily-sync engine like every other category.
    results = garmin.get_race_predictions(cdate, cdate, _type="daily")
    return results[0] if results else None


def _map_race_predictions(cdate: str, raw: dict) -> dict | None:
    if not raw:
        return None
    return {
        "calendar_date": cdate,
        # NOTE: live field names (time5K/time10K/...) differ from the bulk
        # export's (raceTime5K/raceTime10K/...) despite being the same data.
        "time_5k_s": raw.get("time5K"),
        "time_10k_s": raw.get("time10K"),
        "time_half_marathon_s": raw.get("timeHalfMarathon"),
        "time_marathon_s": raw.get("timeMarathon"),
        "source": "api",
    }


def _map_body_composition(cdate: str, raw: dict) -> dict | None:
    entries = (raw or {}).get("dateWeightList") or []
    if not entries:
        return None
    entry = entries[-1]  # most recent measurement of the day
    return {
        "calendar_date": cdate,
        "measurement_timestamp_utc_ms": entry.get("date"),
        "weight_g": entry.get("weight"),
        "bmi": entry.get("bmi"),
        "body_fat_percent": entry.get("bodyFat"),
        "body_water_percent": entry.get("bodyWater"),
        "bone_mass_g": entry.get("boneMass"),
        "muscle_mass_g": entry.get("muscleMass"),
        "visceral_fat_rating": entry.get("visceralFat"),
        "metabolic_age": entry.get("metabolicAge"),
        "source": "api",
    }


def build_specs() -> list[DailyCategorySpec]:
    return [
        DailyCategorySpec("daily_health_metrics", "daily_health_metrics", Garmin.get_user_summary, _map_daily_health, _extra_stress_periods),
        DailyCategorySpec("sleep", "sleep", Garmin.get_sleep_data, _map_sleep),
        DailyCategorySpec("hrv_daily", "hrv_daily", Garmin.get_hrv_data, _map_hrv),
        DailyCategorySpec("training_readiness", "training_readiness", Garmin.get_training_readiness, _map_training_readiness),
        DailyCategorySpec("training_status", "training_status", Garmin.get_training_status, _map_training_status),
        DailyCategorySpec("race_predictions", "race_predictions", _fetch_race_predictions, _map_race_predictions),
        DailyCategorySpec("body_composition", "body_composition", Garmin.get_body_composition, _map_body_composition),
    ]
