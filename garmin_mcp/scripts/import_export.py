"""CLI: seed the local cache from a Garmin bulk data export archive.

Usage:
    garmin-mcp-import-export /path/to/export.zip
"""

from __future__ import annotations

import argparse
import sys

from garmin_mcp.bulk_import.runner import import_export
from garmin_mcp.config import get_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", help="Path to the Garmin 'export all data' zip archive")
    parser.add_argument("--db", help="Override the SQLite DB path (defaults to GARMIN_DB_PATH)")
    args = parser.parse_args()

    config = get_config()
    db_path = args.db or config.db_path

    report = import_export(args.zip_path, db_path)
    print(report.render())

    if any(r.files_found == 0 for r in report.results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
