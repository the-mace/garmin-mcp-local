"""Resume-point helpers backed by the `sync_cursor` table.

'forward' = newest data an incremental sync has reached (moves toward
            today as new data appears).
'backward' = oldest data a backfill has reached (moves toward account
             creation date / activity #1).
"""

from __future__ import annotations

import sqlite3


def get_cursor(conn: sqlite3.Connection, category: str, direction: str) -> str | None:
    row = conn.execute(
        "SELECT cursor_value FROM sync_cursor WHERE category = ? AND direction = ?",
        (category, direction),
    ).fetchone()
    return row[0] if row else None


def set_cursor(
    conn: sqlite3.Connection,
    category: str,
    direction: str,
    cursor_type: str,
    value: str,
) -> None:
    # Commits itself rather than relying on a caller's later commit to sweep
    # it up -- a resume cursor is exactly the kind of write that must never
    # be silently lost if the process exits right after this call.
    conn.execute(
        """
        INSERT INTO sync_cursor (category, direction, cursor_type, cursor_value, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(category, direction) DO UPDATE SET
            cursor_type = excluded.cursor_type,
            cursor_value = excluded.cursor_value,
            updated_at = datetime('now')
        """,
        (category, direction, cursor_type, value),
    )
    conn.commit()
