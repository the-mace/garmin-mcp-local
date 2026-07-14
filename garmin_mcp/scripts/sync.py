"""CLI: incremental sync -- pull everything new since the last successful
sync for every category. Never run automatically; invoke explicitly.

Usage:
    garmin-mcp-sync
"""

from __future__ import annotations

import sys

from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client.factory import build_client
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
from garmin_mcp.sync.runner import run_sync


def main() -> None:
    config = get_config()
    conn = init_db(config.db_path)
    # Only prompt for an MFA code if there's actually a terminal attached
    # (e.g. a scheduled/launchd run has none) -- otherwise input() would
    # hang or raise an unhelpful EOFError instead of the clear "run this
    # interactively once" error build_client already raises.
    garmin = build_client(config, interactive=sys.stdin.isatty())
    rate_limited_client = RateLimitedClient(
        conn,
        RateLimiterConfig(
            min_request_interval_seconds=config.min_request_interval_seconds,
            max_retries=config.max_retries,
        ),
    )

    results = run_sync(conn, garmin, rate_limited_client)

    for category, result in results.items():
        print(f"[{category}] {result}")

    conn.close()


if __name__ == "__main__":
    main()
