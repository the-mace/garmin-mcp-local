"""CLI: email (or print) the daily summary from the local cache. The scheduled
sync sends it on its own when DAILY_SUMMARY_EMAIL is set; this is for sending
one by hand or checking the output.

Usage:
    garmin-mcp-daily-summary [--date YYYY-MM-DD] [--print]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from garmin_mcp.config import get_config
from garmin_mcp.daily_summary import render_daily_summary, send_daily_summary
from garmin_mcp.db.connection import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="day to summarize (default: today)")
    parser.add_argument("--print", action="store_true", help="print to stdout instead of emailing")
    args = parser.parse_args()

    config = get_config()
    day = args.date or date.today()
    conn = init_db(config.db_path)
    try:
        if args.print:
            print(render_daily_summary(conn, day), end="")
            return
        if not config.daily_summary_email:
            sys.exit("DAILY_SUMMARY_EMAIL is not set in .env; use --print to see the summary.")
        if not send_daily_summary(conn, config.daily_summary_email, day):
            sys.exit(1)
        print(f"Sent daily summary for {day}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
