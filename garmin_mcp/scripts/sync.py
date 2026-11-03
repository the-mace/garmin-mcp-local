"""CLI: incremental sync -- pull everything new since the last successful
sync for every category. Never run automatically; invoke explicitly.

Usage:
    garmin-mcp-sync
"""

from __future__ import annotations

import sys
import traceback

from garmin_mcp.alerting import send_alert_email
from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client.factory import build_clients
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
from garmin_mcp.sync.runner import run_sync


def main() -> None:
    try:
        _run()
    except Exception:
        send_alert_email(
            subject="garmin-mcp-local: sync FAILED",
            body=f"garmin-mcp-sync crashed with an unhandled exception:\n\n{traceback.format_exc()}",
        )
        raise


def _run() -> None:
    config = get_config()
    conn = init_db(config.db_path)
    # Only prompt for an MFA code if there's actually a terminal attached
    # (e.g. a scheduled/launchd run has none) -- otherwise input() would
    # hang or raise an unhelpful EOFError instead of the clear "run this
    # interactively once" error build_client already raises (now on first use
    # of the unofficial client, not at startup).
    clients = build_clients(config, interactive=sys.stdin.isatty())
    rate_limited_client = RateLimitedClient(
        conn,
        RateLimiterConfig(
            min_request_interval_seconds=config.min_request_interval_seconds,
            max_retries=config.max_retries,
        ),
    )

    results = run_sync(conn, clients, rate_limited_client)

    for category, result in results.items():
        print(f"[{category}] {result}")

    conn.close()

    # Categories on the official API have already been saved; surface the
    # unofficial login failure through the crash alert in main().
    if clients.login_error is not None:
        raise clients.login_error


if __name__ == "__main__":
    main()
