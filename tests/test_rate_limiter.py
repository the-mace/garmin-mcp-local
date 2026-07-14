"""Tests for the rate-limit backoff behavior: this is one of the two
failure modes the project is explicitly built to avoid (the other is
sync-state idempotency, covered in test_idempotency.py).
"""

from __future__ import annotations

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client.rate_limiter import (
    RateLimitedClient,
    RateLimiterConfig,
    RateLimitExceeded,
    StillCoolingDown,
)


class FakeClock:
    """Deterministic, manually-advanced clock + no-op sleep that actually
    advances the clock, so backoff math can be tested without real delays."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock()


def make_client(conn, clock, **config_kwargs) -> RateLimitedClient:
    config = RateLimiterConfig(**config_kwargs)
    return RateLimitedClient(conn, config, time_fn=clock.time, sleep_fn=clock.sleep)


def test_successful_call_passes_through(conn, clock):
    client = make_client(conn, clock)
    result = client.call("activities", lambda: 42)
    assert result == 42


def test_min_interval_enforced_between_calls(conn, clock):
    client = make_client(conn, clock, min_request_interval_seconds=5.0)
    client.call("activities", lambda: 1)
    client.call("activities", lambda: 2)
    # second call must have been delayed by ~5s via sleep_fn
    assert clock.sleep_calls == [5.0]


def test_no_sleep_when_interval_already_elapsed(conn, clock):
    client = make_client(conn, clock, min_request_interval_seconds=5.0)
    client.call("activities", lambda: 1)
    clock.now += 10.0  # plenty of time has passed
    client.call("activities", lambda: 2)
    assert clock.sleep_calls == []


def test_backoff_grows_exponentially_and_is_capped(conn, clock):
    client = make_client(
        conn, clock, base_backoff_seconds=2.0, max_backoff_seconds=20.0, jitter_fraction=0.0
    )
    assert client.compute_backoff_seconds(1) == pytest.approx(2.0)
    assert client.compute_backoff_seconds(2) == pytest.approx(4.0)
    assert client.compute_backoff_seconds(3) == pytest.approx(8.0)
    assert client.compute_backoff_seconds(4) == pytest.approx(16.0)
    assert client.compute_backoff_seconds(5) == pytest.approx(20.0)  # capped
    assert client.compute_backoff_seconds(10) == pytest.approx(20.0)  # still capped


def test_429_retries_then_succeeds(conn, clock):
    client = make_client(
        conn, clock, min_request_interval_seconds=0.0, base_backoff_seconds=1.0, jitter_fraction=0.0, max_retries=5
    )
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise GarminConnectTooManyRequestsError("429")
        return "ok"

    result = client.call("activities", flaky)
    assert result == "ok"
    assert attempts["n"] == 3
    # two retries happened, each preceded by a sleep for backoff
    assert len(clock.sleep_calls) == 2


def test_exhausted_retries_raises_and_logs_failure(conn, clock):
    client = make_client(conn, clock, base_backoff_seconds=0.1, jitter_fraction=0.0, max_retries=2)

    def always_429():
        raise GarminConnectTooManyRequestsError("429")

    with pytest.raises(RateLimitExceeded):
        client.call("activities", always_429)

    failed = conn.execute(
        "SELECT * FROM sync_log WHERE category = 'activities' AND status = 'failed'"
    ).fetchall()
    assert len(failed) == 1
    assert "max_retries" in failed[0]["warning"]


def test_non_rate_limit_errors_are_not_retried(conn, clock):
    client = make_client(conn, clock, max_retries=5)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("not a rate limit error")

    with pytest.raises(ValueError):
        client.call("activities", boom)
    assert calls["n"] == 1  # no retries attempted


def test_backoff_persisted_and_blocks_next_call_without_hitting_api(conn, clock):
    # Simulate a prior process that hit a 429, persisted a cooldown window,
    # and then died before sleeping through it (crash, OOM-kill, Ctrl-C --
    # the exact scenario this persistence exists for: a normal retry loop
    # always sleeps out whatever backoff it persists, so the only way a
    # *future* process finds an unexpired cooldown is if the process that
    # set it didn't survive to wait it out).
    setup_client = make_client(conn, clock)
    setup_client._persist_backoff("activities", clock.time() + 300.0, "simulated prior 429")

    calls = {"n": 0}

    def always_429():
        calls["n"] += 1
        raise GarminConnectTooManyRequestsError("429")

    fresh_client = make_client(conn, clock, base_backoff_seconds=100.0, max_retries=1)
    with pytest.raises(StillCoolingDown):
        fresh_client.call("activities", always_429)
    assert calls["n"] == 0  # the API was NOT called at all

    # once the cooldown window has elapsed, calls resume normally
    clock.now += 301.0
    assert fresh_client.call("activities", lambda: "ok") == "ok"


def test_different_categories_have_independent_backoff(conn, clock):
    client = make_client(conn, clock, base_backoff_seconds=1000.0, jitter_fraction=0.0, max_retries=1)

    def always_429():
        raise GarminConnectTooManyRequestsError("429")

    with pytest.raises(RateLimitExceeded):
        client.call("activities", always_429)

    # a different category must not be blocked by activities' cooldown
    result = client.call("daily_health_metrics", lambda: "ok")
    assert result == "ok"
