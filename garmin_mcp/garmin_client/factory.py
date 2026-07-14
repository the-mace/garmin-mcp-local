"""Authenticate against Garmin Connect using env-var credentials and a
local token cache, so most runs don't need email/password at all after
the first login.
"""

from __future__ import annotations

from garminconnect import Garmin

from garmin_mcp.config import Config


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
