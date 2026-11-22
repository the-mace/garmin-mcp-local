"""CLI: check `sync_log` for a missed scheduled run or a non-success status
(sync/backfill can fail without crashing -- see garmin_mcp/monitoring.py --
so this is what actually catches that, not just a crashed process), flag
a night with no sleep data (the phone stopped syncing the watch to Garmin
Connect), and warn ahead of the Garmin API key's expiry date. When last
night's sleep is missing it runs one more sync first, so a late wake-up
(watch uploaded after the 08:00 sync) doesn't alert. Sends
one alert email if anything's wrong; sends nothing on a clean check, since
the point is no outbound traffic at all on the happy path.

Usage:
    garmin-mcp-watchdog
"""

from __future__ import annotations

import traceback
from datetime import timedelta

from garmin_mcp.alerting import send_alert_email
from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db
from garmin_mcp.monitoring import (
    WatchdogIssue,
    check_api_key_expiry,
    check_backfill,
    check_connect_upload,
    check_sync,
)
from garmin_mcp.scripts.sync import sync_with_retries

# Both jobs are scheduled daily; this allows one full missed day of slack
# beyond that cadence before flagging staleness.
MAX_AGE = timedelta(hours=27)


def main() -> None:
    config = get_config()
    conn = init_db(config.db_path)
    issues = resync_if_sleep_missing(conn, config)
    issues += check_sync(conn, max_age=MAX_AGE) + check_backfill(conn, max_age=MAX_AGE) + check_connect_upload(conn)
    conn.close()
    issues += check_api_key_expiry(config.api_key_expires)

    if not issues:
        print("OK: sync and backfill are both up to date.")
        return

    for issue in issues:
        print(issue)

    body = "\n".join(str(issue) for issue in issues)
    send_alert_email(subject=f"garmin-mcp-local: {len(issues)} issue(s) detected", body=body)


def resync_if_sleep_missing(conn, config, *, sync=sync_with_retries) -> list[WatchdogIssue]:
    """Run one sync when last night's sleep isn't stored yet. Returns an
    issue only if that sync itself crashed; the checks that follow decide
    whether sleep is still missing."""
    if not check_connect_upload(conn):
        return []
    print("Last night's sleep is missing; running one more sync before checking.")
    try:
        results, _clients = sync(conn, config)
    except Exception:
        return [WatchdogIssue("watchdog", "resync", "failed", traceback.format_exc())]
    for category, result in results.items():
        print(f"[{category}] {result}")
    return []


if __name__ == "__main__":
    main()
