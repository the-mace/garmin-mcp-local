"""Single rate-limited wrapper around the Garmin Connect API client.

Every live API call in this project goes through `RateLimitedClient.call()`
so there is exactly one place that:

  * enforces a minimum delay between consecutive requests,
  * retries 429/403 responses with exponential backoff + jitter, capped at
    `max_retries`, logging a clear failure (not a silent give-up) once
    exhausted,
  * persists the current backoff/cooldown window to `sync_log` so a
    rate-limited run doesn't immediately hammer the API again the next
    time this process (or a fresh one) starts up.

`time_fn` / `sleep_fn` are injectable so tests can exercise the backoff
math without actually sleeping for minutes.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from garminconnect import GarminConnectTooManyRequestsError

_LOGGER = logging.getLogger("garmin_mcp.rate_limiter")

_RATE_LIMIT_STATUS_CODES = {429, 403}


class RateLimitExceeded(Exception):
    """Raised when a call fails after exhausting all retries."""


class StillCoolingDown(Exception):
    """Raised when a persisted backoff window from a previous run hasn't
    elapsed yet -- the caller should not even attempt the request."""

    def __init__(self, category: str, retry_after_seconds: float):
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"{category}: still cooling down from a prior rate limit, "
            f"retry in {retry_after_seconds:.0f}s"
        )


def _status_code_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _is_rate_limited_or_forbidden(exc: BaseException) -> bool:
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return True
    return _status_code_of(exc) in _RATE_LIMIT_STATUS_CODES


@dataclass
class RateLimiterConfig:
    min_request_interval_seconds: float = 1.5
    max_retries: int = 5
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 300.0
    jitter_fraction: float = 0.25


class RateLimitedClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RateLimiterConfig | None = None,
        *,
        time_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.conn = conn
        self.config = config or RateLimiterConfig()
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._last_call_monotonic: float | None = None

    def compute_backoff_seconds(self, attempt: int) -> float:
        """attempt is 1-indexed (first retry = 1)."""
        raw = self.config.base_backoff_seconds * (2 ** (attempt - 1))
        capped = min(raw, self.config.max_backoff_seconds)
        jitter = capped * self.config.jitter_fraction
        return max(0.0, capped + random.uniform(-jitter, jitter))

    def _active_backoff_until(self, category: str) -> float | None:
        row = self.conn.execute(
            """
            SELECT backoff_until_utc_ms FROM sync_log
            WHERE category = ? AND backoff_until_utc_ms IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (category,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return row[0] / 1000.0

    def _persist_backoff(self, category: str, until_epoch_seconds: float, warning: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_log
                (category, run_type, status, warning, backoff_until_utc_ms, completed_at)
            VALUES (?, 'incremental_sync', 'rate_limited', ?, ?, datetime('now'))
            """,
            (category, warning, int(until_epoch_seconds * 1000)),
        )
        self.conn.commit()

    def _log_failure(self, category: str, warning: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_log
                (category, run_type, status, warning, error_message, completed_at)
            VALUES (?, 'incremental_sync', 'failed', ?, ?, datetime('now'))
            """,
            (category, warning, warning),
        )
        self.conn.commit()

    def _respect_min_interval(self) -> None:
        if self._last_call_monotonic is None:
            return
        elapsed = self._time_fn() - self._last_call_monotonic
        remaining = self.config.min_request_interval_seconds - elapsed
        if remaining > 0:
            self._sleep_fn(remaining)

    def call(self, category: str, func: Callable, *args, **kwargs):
        """Invoke `func(*args, **kwargs)` under the rate limiter.

        Raises StillCoolingDown without attempting the call at all if a
        prior run left an unexpired backoff window for this category.
        Raises RateLimitExceeded if retries are exhausted.
        """
        backoff_until = self._active_backoff_until(category)
        now = self._time_fn()
        if backoff_until is not None and backoff_until > now:
            raise StillCoolingDown(category, backoff_until - now)

        attempt = 0
        while True:
            self._respect_min_interval()
            try:
                result = func(*args, **kwargs)
                self._last_call_monotonic = self._time_fn()
                return result
            except Exception as exc:  # noqa: BLE001 -- must inspect any client exception for 429/403
                self._last_call_monotonic = self._time_fn()
                if not _is_rate_limited_or_forbidden(exc):
                    raise
                attempt += 1
                if attempt > self.config.max_retries:
                    warning = (
                        f"{category}: exceeded max_retries={self.config.max_retries} "
                        f"after repeated 429/403 responses ({exc!r})"
                    )
                    _LOGGER.error(warning)
                    self._log_failure(category, warning)
                    raise RateLimitExceeded(warning) from exc

                backoff = self.compute_backoff_seconds(attempt)
                until = self._time_fn() + backoff
                warning = (
                    f"{category}: 429/403 on attempt {attempt}/{self.config.max_retries}, "
                    f"backing off {backoff:.1f}s ({exc!r})"
                )
                _LOGGER.warning(warning)
                self._persist_backoff(category, until, warning)
                self._sleep_fn(backoff)
