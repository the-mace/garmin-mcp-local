"""Import training metrics from Garmin's bulk export.

`training_status` is fed incrementally by several source files that each
only know about a subset of its columns (VO2max from one file, endurance/
hill scores from others, acute load from another). Each importer below
upserts only the columns it owns; SQLite's `ON CONFLICT DO UPDATE SET
col = excluded.col` only touches the columns present in that particular
INSERT, so this never clobbers columns written by a different source file
for the same date.

Sources:
  DI_CONNECT/DI-Connect-Metrics/ActivityVo2Max_*.json
  DI_CONNECT/DI-Connect-Metrics/TrainingHistory_*.json
  DI_CONNECT/DI-Connect-Metrics/EnduranceScore_*.json        (calendarDate is epoch ms)
  DI_CONNECT/DI-Connect-Metrics/HillScore_*.json              (calendarDate is epoch ms)
  DI_CONNECT/DI-Connect-Metrics/MetricsAcuteTrainingLoad_*.json (calendarDate is epoch ms)
  DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_*.json
  DI_CONNECT/DI-Connect-Metrics/TrainingReadinessDTO_*.json
  DI_CONNECT/DI-Connect-Metrics/RunRacePredictions_*.json
"""

from __future__ import annotations

import json
import zipfile
from fnmatch import fnmatch

from garmin_mcp.db.connection import upsert
from garmin_mcp.bulk_import.report import ImportReport
from garmin_mcp.bulk_import.units import epoch_ms_to_date

GLOBS = {
    "vo2max": "DI_CONNECT/DI-Connect-Metrics/ActivityVo2Max_*.json",
    "training_history": "DI_CONNECT/DI-Connect-Metrics/TrainingHistory_*.json",
    "endurance_score": "DI_CONNECT/DI-Connect-Metrics/EnduranceScore_*.json",
    "hill_score": "DI_CONNECT/DI-Connect-Metrics/HillScore_*.json",
    "acute_load": "DI_CONNECT/DI-Connect-Metrics/MetricsAcuteTrainingLoad_*.json",
    "max_met": "DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_*.json",
    "training_readiness": "DI_CONNECT/DI-Connect-Metrics/TrainingReadinessDTO_*.json",
    "race_predictions": "DI_CONNECT/DI-Connect-Metrics/RunRacePredictions_*.json",
}


def _load_all(zf: zipfile.ZipFile, glob: str) -> list[dict]:
    records = []
    for name in zf.namelist():
        if fnmatch(name, glob):
            with zf.open(name) as f:
                records.extend(json.load(f))
    return records


def _import_vo2max(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.vo2max")
    records = _load_all(zf, GLOBS["vo2max"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["vo2max"])])
    result.records_seen = len(records)
    for r in records:
        sport = (r.get("sport") or "").upper()
        value = r.get("vo2MaxValue")
        calendar_date = r.get("calendarDate")
        if not calendar_date or value is None:
            result.skipped += 1
            continue
        if "RUN" in sport:
            column = "vo2max_running"
        elif "CYCL" in sport or "BIK" in sport:
            column = "vo2max_cycling"
        else:
            result.skipped += 1
            result.notes.append(f"unmapped VO2max sport '{sport}' on {calendar_date} -- not imported")
            continue
        upsert(conn, "training_status", {"calendar_date": calendar_date, column: value, "source": "csv_export"}, ["calendar_date"])
        result.rows_written += 1


def _import_training_history(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.status")
    records = _load_all(zf, GLOBS["training_history"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["training_history"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = r.get("calendarDate")
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_status",
            {
                "calendar_date": calendar_date,
                "training_status": r.get("trainingStatus"),
                "fitness_trend": r.get("fitnessLevelTrend"),
                "source": "csv_export",
            },
            ["calendar_date"],
        )
        result.rows_written += 1


def _import_endurance_score(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.endurance_score")
    records = _load_all(zf, GLOBS["endurance_score"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["endurance_score"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = epoch_ms_to_date(r.get("calendarDate"))
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_status",
            {"calendar_date": calendar_date, "endurance_score": r.get("overallScore"), "source": "csv_export"},
            ["calendar_date"],
        )
        result.rows_written += 1


def _import_hill_score(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.hill_score")
    records = _load_all(zf, GLOBS["hill_score"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["hill_score"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = epoch_ms_to_date(r.get("calendarDate"))
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_status",
            {
                "calendar_date": calendar_date,
                "hill_strength_score": r.get("strengthScore"),
                "hill_endurance_score": r.get("enduranceScore"),
                "hill_overall_score": r.get("overallScore"),
                "hill_classification_id": r.get("hillScoreClassificationId"),
                "source": "csv_export",
            },
            ["calendar_date"],
        )
        result.rows_written += 1


def _import_acute_load(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.acute_load")
    records = _load_all(zf, GLOBS["acute_load"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["acute_load"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = epoch_ms_to_date(r.get("calendarDate"))
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_status",
            {
                "calendar_date": calendar_date,
                "acute_load": r.get("dailyTrainingLoadAcute"),
                "chronic_load": r.get("dailyTrainingLoadChronic"),
                "acwr_percent": r.get("acwrPercent"),
                "acwr_status": r.get("acwrStatus"),
                "source": "csv_export",
            },
            ["calendar_date"],
        )
        result.rows_written += 1


def _import_max_met(zf, conn, report: ImportReport) -> None:
    result = report.category("training_status.max_met")
    records = _load_all(zf, GLOBS["max_met"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["max_met"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = r.get("calendarDate")
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_status",
            {"calendar_date": calendar_date, "max_met": r.get("maxMet"), "source": "csv_export"},
            ["calendar_date"],
        )
        result.rows_written += 1


def _import_training_readiness(zf, conn, report: ImportReport) -> None:
    result = report.category("training_readiness")
    records = _load_all(zf, GLOBS["training_readiness"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["training_readiness"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = r.get("calendarDate")
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "training_readiness",
            {
                "calendar_date": calendar_date,
                "score": r.get("score"),
                "level": r.get("level"),
                "feedback_long": r.get("feedbackLong"),
                "feedback_short": r.get("feedbackShort"),
                "sleep_score": r.get("sleepScore"),
                "sleep_score_factor_percent": r.get("sleepScoreFactorPercent"),
                "recovery_time_minutes": r.get("recoveryTime"),
                "recovery_time_factor_percent": r.get("recoveryTimeFactorPercent"),
                "acwr_factor_percent": r.get("acwrFactorPercent"),
                "stress_history_factor_percent": r.get("stressHistoryFactorPercent"),
                "hrv_factor_percent": r.get("hrvFactorPercent"),
                "hrv_weekly_average": r.get("hrvWeeklyAverage"),
                "sleep_history_factor_percent": r.get("sleepHistoryFactorPercent"),
                "source": "csv_export",
            },
            ["calendar_date"],
        )
        result.rows_written += 1

        if r.get("hrvWeeklyAverage") is not None:
            upsert(
                conn,
                "hrv_daily",
                {
                    "calendar_date": calendar_date,
                    "weekly_avg": r.get("hrvWeeklyAverage"),
                    "source": "csv_export_approx",
                },
                ["calendar_date"],
            )


def _import_race_predictions(zf, conn, report: ImportReport) -> None:
    result = report.category("race_predictions")
    records = _load_all(zf, GLOBS["race_predictions"])
    result.files_found = len([n for n in zf.namelist() if fnmatch(n, GLOBS["race_predictions"])])
    result.records_seen = len(records)
    for r in records:
        calendar_date = r.get("calendarDate")
        if not calendar_date:
            result.skipped += 1
            continue
        upsert(
            conn,
            "race_predictions",
            {
                "calendar_date": calendar_date,
                "time_5k_s": r.get("raceTime5K"),
                "time_10k_s": r.get("raceTime10K"),
                "time_half_marathon_s": r.get("raceTimeHalf"),
                "time_marathon_s": r.get("raceTimeMarathon"),
                "source": "csv_export",
            },
            ["calendar_date"],
        )
        result.rows_written += 1


def import_training_metrics(zf: zipfile.ZipFile, conn, report: ImportReport) -> None:
    _import_vo2max(zf, conn, report)
    _import_training_history(zf, conn, report)
    _import_endurance_score(zf, conn, report)
    _import_hill_score(zf, conn, report)
    _import_acute_load(zf, conn, report)
    _import_max_met(zf, conn, report)
    _import_training_readiness(zf, conn, report)
    _import_race_predictions(zf, conn, report)
    conn.commit()

    report.flag_not_populated(
        "hrv_daily (last_night_avg, baseline, status)",
        "the bulk export has no dedicated nightly-HRV file for this account -- "
        "only a weekly average is derivable (from TrainingReadinessDTO), stored "
        "with source='csv_export_approx'. Run a live sync to backfill nightly detail.",
    )
    report.flag_not_populated(
        "body_composition",
        "no weight/body-composition file found in this export (no Garmin Index "
        "scale data on this account). Populate via live API sync if you have scale data.",
    )
