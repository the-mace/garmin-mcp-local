"""Authenticate against Garmin Connect using env-var credentials and a
local token cache, so most runs don't need email/password at all after
the first login, and build the official Personal API client when
`GARMIN_API_KEY` is set.
"""

from __future__ import annotations

import time
from typing import Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.config import Config
from garmin_mcp.garmin_client.personal_api import PersonalApiClient
from garmin_mcp.retry import is_transient, retry_call

# Transient login failures (SSO 5xx, timeouts) get 30s, 60s, 120s waits.
# Bad credentials, MFA and login rate limits are never retried: hammering
# Garmin's SSO is how accounts get locked.
LOGIN_RETRIES = 3
LOGIN_BASE_DELAY_SECONDS = 30.0
LOGIN_MAX_DELAY_SECONDS = 120.0


def _login_is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError)):
        return False
    if "MFA" in str(exc):
        return False
    return is_transient(exc)


def _prompt_mfa_code() -> str:
    return input("Enter the MFA code Garmin just sent you (email/SMS): ").strip()


def build_client(
    config: Config,
    *,
    interactive: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    garmin_cls: Callable[..., Garmin] = Garmin,
) -> Garmin:
    """Log in and return an authenticated Garmin client.

    `interactive=True` (used by the CLI scripts, run from a real terminal)
    lets the user type an MFA code if Garmin requires one, and the
    resulting session token gets persisted to `config.token_store` so
    future runs -- including the long-running MCP server, which passes
    `interactive=False` since it has no TTY to prompt on -- don't need to
    log in (or handle MFA) again until the token expires.

    Transient failures are retried with a fresh client each time (see
    LOGIN_RETRIES).
    """
    config.token_store.mkdir(parents=True, exist_ok=True)

    def attempt() -> Garmin:
        client = garmin_cls(
            config.garmin_email,
            config.garmin_password,
            prompt_mfa=_prompt_mfa_code if interactive else None,
        )
        client.login(tokenstore=str(config.token_store))
        return client

    try:
        return retry_call(
            attempt,
            label="garminconnect login",
            retries=LOGIN_RETRIES,
            base_delay=LOGIN_BASE_DELAY_SECONDS,
            max_delay=LOGIN_MAX_DELAY_SECONDS,
            is_retryable=_login_is_retryable,
            sleep_fn=sleep_fn,
        )
    except Exception as exc:
        if "MFA" in str(exc) and not interactive:
            raise RuntimeError(
                "Garmin requires an MFA code and this isn't an interactive session. "
                "Run `garmin-mcp-sync` (or `garmin-mcp-backfill`) directly in a terminal "
                "once to complete login interactively -- the session token gets cached "
                "afterward, so the MCP server won't need to prompt again."
            ) from exc
        raise


class GarminClients:
    """Both clients a sync run may use. `personal` is the official API
    (None without an API key); `garmin` is the unofficial `garminconnect`
    session, still needed for categories the official API doesn't serve.

    `garmin` logs in on first use, so a broken unofficial login only fails
    the categories that need it instead of the whole run. A failed login is
    remembered and re-raised rather than retried per date, which would
    hammer Garmin's SSO and risk a lockout.
    """

    def __init__(self, personal: PersonalApiClient | None, garmin_factory: Callable[[], Garmin]):
        self.personal = personal
        self._garmin_factory = garmin_factory
        self._garmin: Garmin | None = None
        self._login_error: Exception | None = None

    @property
    def login_error(self) -> Exception | None:
        return self._login_error

    @property
    def garmin(self) -> Garmin:
        if self._login_error is not None:
            raise self._login_error
        if self._garmin is None:
            try:
                self._garmin = self._garmin_factory()
            except Exception as exc:
                self._login_error = exc
                raise
        return self._garmin


def build_clients(config: Config, *, interactive: bool = False) -> GarminClients:
    personal = PersonalApiClient(config.api_key) if config.api_key else None
    return GarminClients(personal, lambda: build_client(config, interactive=interactive))
