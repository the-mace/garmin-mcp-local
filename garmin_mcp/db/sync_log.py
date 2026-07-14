"""Shared helper for writing to the sync_log audit trail."""

from __future__ import annotations

import sqlite3


def log_sync_run(
    conn: sqlite3.Connection,
    *,
    category: str,
    run_type: str,
    status: str,
    records_expected: int | None = None,
    records_fetched: int | None = None,
    warning: str | None = None,
    error_message: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    cursor_type: str | None = None,
    last_activity_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_log
            (category, run_type, cursor_type, range_start, range_end, last_activity_id,
             status, records_expected, records_fetched, warning, error_message, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            category,
            run_type,
            cursor_type,
            range_start,
            range_end,
            last_activity_id,
            status,
            records_expected,
            records_fetched,
            warning,
            error_message,
        ),
    )
    conn.commit()
