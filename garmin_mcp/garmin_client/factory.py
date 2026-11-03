"""Authenticate against Garmin Connect using env-var credentials and a
local token cache, so most runs don't need email/password at all after
the first login, and build the official Personal API client when
`GARMIN_API_KEY` is set.
"""

from __future__ import annotations

from typing import Callable

from garminconnect import Garmin

from garmin_mcp.config import Config
from garmin_mcp.garmin_client.personal_api import PersonalApiClient


def _prompt_mfa_code() -> str:
    return input("Enter the MFA code Garmin just sent you (email/SMS): ").strip()


def build_client(config: Config, *, interactive: bool = False) -> Garmin:
    """Log in and return an authenticated Garmin client.

    `interactive=True` (used by the CLI scripts, run from a real terminal)
    lets the user type an MFA code if Garmin requires one, and the
    resulting session token gets persisted to `config.token_store` so
    future runs -- including the long-running MCP server, which passes
    `interactive=False` since it has no TTY to prompt on -- don't need to
    log in (or handle MFA) again until the token expires.
    """
    client = Garmin(
        config.garmin_email,
        config.garmin_password,
        prompt_mfa=_prompt_mfa_code if interactive else None,
    )
    config.token_store.mkdir(parents=True, exist_ok=True)
    try:
        client.login(tokenstore=str(config.token_store))
    except Exception as exc:
        if "MFA" in str(exc) and not interactive:
            raise RuntimeError(
                "Garmin requires an MFA code and this isn't an interactive session. "
                "Run `garmin-mcp-sync` (or `garmin-mcp-backfill`) directly in a terminal "
                "once to complete login interactively -- the session token gets cached "
                "afterward, so the MCP server won't need to prompt again."
            ) from exc
        raise
    return client


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
