"""CLI: run one controlled batch of API-driven backfill per category,
walking further back into history than the bulk export (or a prior
backfill run) reached. Safe to run repeatedly / on a schedule -- each
invocation picks up from the persisted backward cursor.

Usage:
    garmin-mcp-backfill [--batch-days 30] [--earliest-date 2010-01-01] [--category hrv_daily --category body_composition]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date

from garmin_mcp.alerting import send_alert_email
from garmin_mcp.config import get_config
from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client.factory import build_client
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
from garmin_mcp.sync.runner import run_backfill


def main() -> None:
    try:
        _run()
    except Exception:
        send_alert_email(
            subject="garmin-mcp-local: backfill FAILED",
            body=f"garmin-mcp-backfill crashed with an unhandled exception:\n\n{traceback.format_exc()}",
        )
        raise


def _run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-days", type=int, default=30)
    parser.add_argument("--earliest-date", type=str, default="2000-01-01")
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help="Restrict backfill to this category (repeatable). Omit to backfill every category. "
        "One of: activities, daily_health_metrics, sleep, hrv_daily, training_readiness, "
        "training_status, race_predictions, body_composition.",
    )
    args = parser.parse_args()

    config = get_config()
    conn = init_db(config.db_path)
    garmin = build_client(config, interactive=sys.stdin.isatty())
    rate_limited_client = RateLimitedClient(
        conn,
        RateLimiterConfig(
            min_request_interval_seconds=config.min_request_interval_seconds,
            max_retries=config.max_retries,
        ),
    )

    results = run_backfill(
        conn,
        garmin,
        rate_limited_client,
        batch_days=args.batch_days,
        earliest_date=date.fromisoformat(args.earliest_date),
        categories=set(args.categories) if args.categories else None,
    )

    for category, result in results.items():
        print(f"[{category}] {result}")

    conn.close()


if __name__ == "__main__":
    main()
