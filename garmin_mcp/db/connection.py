"""SQLite connection factory, schema bootstrap, and a generic idempotent
upsert helper used by every importer/syncer so "re-run never duplicates"
is enforced in one place rather than reimplemented per table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    schema_sql = _SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    apply_schema(conn)
    return conn


_table_columns_cache: dict[str, set[str]] = {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in _table_columns_cache:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        _table_columns_cache[table] = {r[1] for r in rows}
    return _table_columns_cache[table]


def upsert(
    conn: sqlite3.Connection,
    table: str,
    row: dict,
    conflict_columns: list[str],
    *,
    touch_updated_at: bool = True,
) -> None:
    """Idempotent INSERT ... ON CONFLICT DO UPDATE.

    `row` keys become columns. On a natural-key conflict, every non-key
    column is overwritten with the new value (upsert semantics: latest
    fetch wins), so re-running an import or sync is always safe to repeat
    and never creates duplicate rows.
    """
    if not row:
        raise ValueError(f"upsert into {table} called with an empty row")

    columns = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    column_list = ", ".join(columns)

    update_columns = [c for c in columns if c not in conflict_columns]
    set_clause_parts = [f"{c} = excluded.{c}" for c in update_columns]
    if touch_updated_at and "updated_at" not in columns and "updated_at" in _table_columns(conn, table):
        set_clause_parts.append("updated_at = datetime('now')")
    set_clause = ", ".join(set_clause_parts)

    conflict_target = ", ".join(conflict_columns)

    if set_clause:
        sql = (
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO UPDATE SET {set_clause}"
        )
    else:
        # Table has nothing but key columns (e.g. a pure link table) --
        # conflict means the link already exists, which is a no-op.
        sql = (
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO NOTHING"
        )

    conn.execute(sql, row)
