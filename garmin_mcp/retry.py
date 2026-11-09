"""Shared retry helpers for every external action this project takes
(Garmin HTTP calls, Garmin login, a whole sync run, sending mail).

Three layers use them:

  * `RateLimitedClient.call()` retries each API request: 429/403 with a
    persisted cooldown, transient failures (5xx, timeouts, connection
    errors) with a short in-process backoff.
  * `build_client()` retries the `garminconnect` login on transient
    failures only -- never on bad credentials, MFA or a login rate limit,
    where retrying risks an SSO lockout.
  * `run_with_retries()` re-runs a whole scheduled sync a few minutes later
    when a run crashed or left transient failures behind, for outages that
    outlast the per-call backoff. Safe because every sync is idempotent and
    cursor-based.

`sleep_fn` is injectable everywhere so tests never actually sleep.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import httpx
import requests

_LOGGER = logging.getLogger("garmin_mcp.retry")

T = TypeVar("T")

# 5xx responses worth retrying. 501 (not implemented) and 505 never heal.
TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 520, 521, 522, 523, 524}

_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    httpx.TransportError,  # timeouts, connect/read errors, protocol errors
    requests.ConnectionError,
    requests.Timeout,
    ConnectionError,
    TimeoutError,
)


def status_code_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient(exc: BaseException) -> bool:
    """True for failures that a later attempt can plausibly fix: a retryable
    5xx or a network-level error anywhere in the exception chain (libraries
    such as garminconnect wrap the original error)."""
    for link in _exception_chain(exc):
        if status_code_of(link) in TRANSIENT_STATUS_CODES:
            return True
        if isinstance(link, _TRANSIENT_EXCEPTION_TYPES):
            return True
    return False


def backoff_seconds(
    attempt: int, base: float, cap: float, jitter_fraction: float = 0.25
) -> float:
    """Exponential backoff for a 1-indexed retry attempt, capped, +/- jitter."""
    capped = min(base * (2 ** (attempt - 1)), cap)
    jitter = capped * jitter_fraction
    return max(0.0, capped + random.uniform(-jitter, jitter))


def retry_call(
    func: Callable[[], T],
    *,
    label: str,
    retries: int,
    base_delay: float,
    max_delay: float,
    is_retryable: Callable[[BaseException], bool] = is_transient,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Call `func()`, retrying up to `retries` extra times while the raised
    exception is retryable. The last exception propagates unchanged."""
    attempt = 0
    while True:
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised if not retryable
            attempt += 1
            if attempt > retries or not is_retryable(exc):
                raise
            delay = backoff_seconds(attempt, base_delay, max_delay)
            _LOGGER.warning(
                "%s failed (attempt %d/%d), retrying in %.0fs: %r",
                label, attempt, retries + 1, delay, exc,
            )
            sleep_fn(delay)


def run_with_retries(
    attempt_fn: Callable[[], T],
    *,
    label: str,
    retries: int,
    delay_seconds: float,
    needs_retry: Callable[[T], str | None],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Run a whole job up to `retries + 1` times.

    Retries when `attempt_fn` raises a transient error, or returns a result
    for which `needs_retry(result)` gives a reason string. Non-transient
    exceptions propagate at once. After the last attempt the result is
    returned (or the exception raised) as-is, so callers still report it.
    The delay doubles between attempts.
    """
    for attempt in range(retries + 1):
        last = attempt == retries
        delay = delay_seconds * (2 ** attempt)
        try:
            result = attempt_fn()
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised if not retryable
            if last or not is_transient(exc):
                raise
            reason = f"crashed with a transient error: {exc!r}"
        else:
            reason = needs_retry(result)
            if reason is None or last:
                return result
        print(
            f"{label}: attempt {attempt + 1}/{retries + 1} {reason}; "
            f"retrying in {delay:.0f}s",
            flush=True,
        )
        sleep_fn(delay)
    raise AssertionError("unreachable")
