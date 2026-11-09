"""CLI: evening activities-only sync -- fetch today's workouts, then email
them under subject "garmin activities YYYY-MM-DD". Does not sync sleep,
health, or other wellness categories.

Usage:
    garmin-mcp-activities-sync
    garmin-mcp-activities-sync --print
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date

from garmin_mcp.alerting import send_alert_email
from garmin_mcp.config import get_config
from garmin_mcp.daily_summary import render_activities_summary, send_activities_summary
from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client.factory import build_clients
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
from garmin_mcp.sync.runner import run_todays_activities_sync


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--print",
        action="store_true",
        help="after syncing, print the activities summary instead of emailing it",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="local calendar day to sync/summarize (default: today)",
    )
    args = parser.parse_args()

    try:
        _run(day=args.date, print_only=args.print)
    except Exception:
        send_alert_email(
            subject="garmin-mcp-local: activities-sync FAILED",
            body=f"garmin-mcp-activities-sync crashed with an unhandled exception:\n\n{traceback.format_exc()}",
        )
        raise


def _run(*, day: date | None, print_only: bool) -> None:
    config = get_config()
    day = day or date.today()
    conn = init_db(config.db_path)
    clients = build_clients(config, interactive=sys.stdin.isatty())
    rate_limited_client = RateLimitedClient(
        conn,
        RateLimiterConfig(
            min_request_interval_seconds=config.min_request_interval_seconds,
            max_retries=config.max_retries,
        ),
    )

    results = run_todays_activities_sync(conn, clients, rate_limited_client, today=day)
    for category, result in results.items():
        print(f"[{category}] {result}")

    if print_only:
        print(render_activities_summary(conn, day), end="")
    elif config.daily_summary_email:
        send_activities_summary(conn, config.daily_summary_email, day)

    conn.close()

    if clients.login_error is not None:
        raise clients.login_error


if __name__ == "__main__":
    main()
