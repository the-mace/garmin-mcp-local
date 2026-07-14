"""Top-level orchestration for importing a Garmin bulk export archive.

Despite being commonly called a "CSV export" request, Garmin's "export all
data" delivery is a zip of JSON files organized under DI_CONNECT/ by data
category, not CSV. This module parses that zip directly (no need to
pre-extract) and maps each category onto the local schema.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from garmin_mcp.bulk_import.activities import import_activities
from garmin_mcp.bulk_import.daily_health import import_daily_health, import_sleep
from garmin_mcp.bulk_import.gear import import_gear
from garmin_mcp.bulk_import.report import ImportReport
from garmin_mcp.bulk_import.training_metrics import import_training_metrics
from garmin_mcp.db.connection import init_db


def _log_sync_run(conn: sqlite3.Connection, category: str, result) -> None:
    status = "success"
    warning = None
    if result.files_found == 0:
        status = "failed"
        warning = "no matching files found in export"
    elif result.skipped > 0:
        status = "partial"
        warning = f"{result.skipped} record(s) skipped -- see import report notes"

    conn.execute(
        """
        INSERT INTO sync_log
            (category, run_type, status, records_expected, records_fetched, warning, completed_at)
        VALUES (?, 'bulk_import', ?, ?, ?, ?, datetime('now'))
        """,
        (category, status, result.records_seen, result.rows_written, warning),
    )


def import_export(zip_path: str | Path, db_path: str | Path) -> ImportReport:
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"Garmin export archive not found: {zip_path}")

    conn = init_db(db_path)
    report = ImportReport()

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Activities before gear: gear-activity links need the activity
            # row to already exist (foreign key + explicit existence check).
            import_activities(zf, conn, report)
            import_gear(zf, conn, report)
            import_daily_health(zf, conn, report)
            import_sleep(zf, conn, report)
            import_training_metrics(zf, conn, report)

        for category, result in report.results.items():
            _log_sync_run(conn, category, result)
        conn.commit()
    finally:
        conn.close()

    return report
