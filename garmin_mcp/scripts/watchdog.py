"""CLI: check `sync_log` for a missed scheduled run or a non-success status
(sync/backfill can fail without crashing -- see garmin_mcp/monitoring.py --
so this is what actually catches that, not just a crashed process). Sends
one alert email if anything's wrong; sends nothing on a clean check, since
the point is no outbound traffic at all on the happy path.

Usage:
    garmin-mcp-watchdog
"""

from __future__ import annotations

from datetime import timedelta

from garmin_mcp.alerting import send_alert_email
from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db
from garmin_mcp.monitoring import check_backfill, check_sync

# Both jobs are scheduled daily; this allows one full missed day of slack
# beyond that cadence before flagging staleness.
MAX_AGE = timedelta(hours=27)


def main() -> None:
    config = get_config()
    conn = init_db(config.db_path)
    issues = check_sync(conn, max_age=MAX_AGE) + check_backfill(conn, max_age=MAX_AGE)
    conn.close()

    if not issues:
        print("OK: sync and backfill are both up to date.")
        return

    for issue in issues:
        print(issue)

    body = "\n".join(str(issue) for issue in issues)
    send_alert_email(subject=f"garmin-mcp-local: {len(issues)} issue(s) detected", body=body)


if __name__ == "__main__":
    main()
