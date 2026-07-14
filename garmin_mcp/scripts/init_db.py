"""CLI: create the local SQLite cache + apply schema, without importing
or syncing anything. Mostly useful for scripting/tests.

Usage:
    garmin-mcp-init-db
"""

from __future__ import annotations

from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db


def main() -> None:
    config = get_config()
    conn = init_db(config.db_path)
    conn.close()
    print(f"Initialized schema at {config.db_path}")


if __name__ == "__main__":
    main()
