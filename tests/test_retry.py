"""Retries for external actions: transient Garmin API errors, login, whole
sync runs and mail. Regression for the 2026-11-09 08:00 sync, which crashed
on a single 504 Gateway Timeout from the Personal API token endpoint."""

from __future__ import annotations

import subprocess

import httpx
import pytest
import requests
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from garmin_mcp import alerting
from garmin_mcp.config import Config
from garmin_mcp.db.connection import init_db
from garmin_mcp.garmin_client import factory
from garmin_mcp.garmin_client.factory import GarminClients
from garmin_mcp.garmin_client.personal_api import TOKEN_PATH, PersonalApiClient
from garmin_mcp.garmin_client.rate_limiter import RateLimitedClient, RateLimiterConfig
from garmin_mcp.retry import is_transient, run_with_retries
from garmin_mcp.sync.activities_sync import incremental_sync_activities
from garmin_mcp.sync.runner import sync_needs_retry


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock()


def _limiter(conn, clock, **kw):
    cfg = RateLimiterConfig(min_request_interval_seconds=0.0, jitter_fraction=0.0, **kw)
    return RateLimitedClient(conn, cfg, time_fn=clock.time, sleep_fn=clock.sleep)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid")
    return httpx.HTTPStatusError(f"Server error {status}", request=request, response=httpx.Response(status, request=request))


def _flaky(failures: list[BaseException], value="ok"):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return value

    return fn, calls


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_status_error(504), True),
        (_status_error(503), True),
        (_status_error(404), False),
        (_status_error(400), False),
        (httpx.ReadTimeout("slow"), True),
        (httpx.ConnectError("refused"), True),
        (requests.ConnectionError("reset"), True),
        (ValueError("bad payload"), False),
    ],
)
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected


def test_is_transient_follows_wrapped_cause():
    # garminconnect wraps network errors: `raise GarminConnectConnectionError(...) from e`
    try:
        try:
            raise requests.ConnectionError("reset")
        except requests.ConnectionError as inner:
            raise GarminConnectConnectionError("Login failed") from inner
    except GarminConnectConnectionError as wrapped:
        assert is_transient(wrapped)


# --- rate limiter ---------------------------------------------------------


def test_transient_error_is_retried_without_persisting_a_cooldown(conn, clock):
    fn, calls = _flaky([_status_error(504), _status_error(502)])
    client = _limiter(conn, clock, transient_base_backoff_seconds=5.0)
    assert client.call("activities", fn) == "ok"
    assert len(calls) == 3
    assert clock.sleeps == [5.0, 10.0]
    # an outage must not block the next run the way a 429 cooldown does
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


def test_transient_retries_exhausted_reraises_original(conn, clock):
    fn, calls = _flaky([_status_error(504)] * 10)
    client = _limiter(conn, clock, transient_max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        client.call("activities", fn)
    assert len(calls) == 3


def test_sustained_outage_stops_retrying_each_call(conn, clock):
    client = _limiter(conn, clock, transient_max_retries=2, outage_after_exhausted_calls=2)
    for _ in range(2):
        fn, calls = _flaky([_status_error(504)] * 10)
        with pytest.raises(httpx.HTTPStatusError):
            client.call("sleep", fn)
        assert len(calls) == 3
    fn, calls = _flaky([_status_error(504)] * 10)
    with pytest.raises(httpx.HTTPStatusError):
        client.call("sleep", fn)
    assert len(calls) == 1  # Garmin looks down: fail fast
    fn, calls = _flaky([])
    assert client.call("sleep", fn) == "ok"  # a success resets the breaker
    fn, calls = _flaky([_status_error(504)])
    assert client.call("sleep", fn) == "ok"
    assert len(calls) == 2


def test_non_transient_error_is_not_retried(conn, clock):
    fn, calls = _flaky([_status_error(404)])
    with pytest.raises(httpx.HTTPStatusError):
        _limiter(conn, clock).call("activities", fn)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_token_endpoint_504_recovers(conn, clock):
    """The exact failure from the 2026-11-09 alert."""
    token_hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_hits.append(1)
            if len(token_hits) == 1:
                return httpx.Response(504, text="Gateway Timeout")
            return httpx.Response(200, text='"jwt"')
        return httpx.Response(200, json=[{"activityId": 1}])

    personal = PersonalApiClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    result = _limiter(conn, clock).call("activities", personal.get_activities, 0, 50)
    assert result == [{"activityId": 1}]
    assert len(token_hits) == 2


# --- activities sync no longer crashes the run ----------------------------


class DownPersonal:
    def get_activities(self, start, limit):
        raise _status_error(504)


def test_activities_page_failure_is_partial_not_a_crash(conn, clock):
    limiter = _limiter(conn, clock, transient_max_retries=1)
    result = incremental_sync_activities(limiter, GarminClients(DownPersonal(), lambda: None), conn)
    assert result.fetch_failed
    assert result.transient_failures == 1
    status, warning = conn.execute(
        "SELECT status, warning FROM sync_log WHERE category = 'activities' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert status == "partial"
    assert "504" in warning


# --- whole-run retry ------------------------------------------------------


def test_run_with_retries_reruns_until_clean(clock):
    outcomes = iter(["transient", "transient", None])
    attempts = []

    def attempt():
        attempts.append(1)
        return next(outcomes)

    result = run_with_retries(
        attempt, label="t", retries=3, delay_seconds=60, needs_retry=lambda r: r, sleep_fn=clock.sleep
    )
    assert result is None
    assert len(attempts) == 3
    assert clock.sleeps == [60, 120]


def test_run_with_retries_returns_last_result_when_exhausted(clock):
    result = run_with_retries(
        lambda: "still failing", label="t", retries=1, delay_seconds=1, needs_retry=lambda r: r, sleep_fn=clock.sleep
    )
    assert result == "still failing"
    assert clock.sleeps == [1]


def test_run_with_retries_retries_transient_crash_only(clock):
    fn, calls = _flaky([_status_error(504)])
    assert run_with_retries(
        fn, label="t", retries=2, delay_seconds=1, needs_retry=lambda r: None, sleep_fn=clock.sleep
    ) == "ok"
    assert len(calls) == 2

    fn, calls = _flaky([ValueError("bug")])
    with pytest.raises(ValueError):
        run_with_retries(fn, label="t", retries=2, delay_seconds=1, needs_retry=lambda r: None, sleep_fn=clock.sleep)
    assert len(calls) == 1


def test_sync_needs_retry_ignores_non_transient_failures():
    class R:
        def __init__(self, n):
            self.transient_failures = n

    clean = GarminClients(None, lambda: None)
    assert sync_needs_retry({"sleep": R(0)}, clean) is None
    assert "sleep" in sync_needs_retry({"sleep": R(2)}, clean)

    def bad_login():
        raise GarminConnectAuthenticationError("wrong password")

    clients = GarminClients(None, bad_login)
    with pytest.raises(GarminConnectAuthenticationError):
        clients.garmin
    assert sync_needs_retry({}, clients) is None


# --- login ----------------------------------------------------------------


def _config(tmp_path) -> Config:
    return Config(
        garmin_email="e", garmin_password="p", token_store=tmp_path / "tokens", db_path=tmp_path / "db",
        min_request_interval_seconds=0, max_retries=1, alert_email_to=None, api_key=None,
        api_key_expires=None, daily_summary_email=None,
    )


def _fake_garmin_cls(failures):
    created = []

    class FakeGarmin:
        def __init__(self, *a, **kw):
            created.append(self)

        def login(self, tokenstore):
            if len(created) <= len(failures):
                raise failures[len(created) - 1]

    return FakeGarmin, created


def test_login_retries_transient_failures(tmp_path, clock):
    cls, created = _fake_garmin_cls([requests.ConnectionError("reset"), _status_error(503)])
    client = factory.build_client(_config(tmp_path), sleep_fn=clock.sleep, garmin_cls=cls)
    assert client is created[-1]
    assert len(created) == 3
    assert len(clock.sleeps) == 2


def test_login_never_retries_auth_errors(tmp_path, clock):
    cls, created = _fake_garmin_cls([GarminConnectAuthenticationError("401")])
    with pytest.raises(GarminConnectAuthenticationError):
        factory.build_client(_config(tmp_path), sleep_fn=clock.sleep, garmin_cls=cls)
    assert len(created) == 1
    assert clock.sleeps == []


# --- mail -----------------------------------------------------------------


def test_send_email_retries_then_succeeds(clock):
    fn_calls = []

    def run(*a, **kw):
        fn_calls.append(1)
        if len(fn_calls) == 1:
            raise subprocess.CalledProcessError(75, "mail")

    assert alerting.send_email("a@b", "s", "b", sleep_fn=clock.sleep, run_fn=run)
    assert len(fn_calls) == 2
    assert clock.sleeps == [alerting.MAIL_RETRY_DELAYS_SECONDS[0]]


def test_send_email_gives_up_without_raising(clock):
    def run(*a, **kw):
        raise subprocess.TimeoutExpired("mail", 30)

    assert not alerting.send_email("a@b", "s", "b", sleep_fn=clock.sleep, run_fn=run)
    assert len(clock.sleeps) == alerting.MAIL_ATTEMPTS - 1
