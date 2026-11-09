"""Local-only failure alerting.

Sends mail via the system `mail` command, which this machine already routes
through Postfix to a real mail provider (same path other local automations
here use). Deliberately not a hosted heartbeat/monitoring service: the only
network traffic this ever generates is the alert itself, sent only when
there's actually something to report -- never a periodic ping that would
otherwise leak "is this machine online right now" to a third party.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Callable

from garmin_mcp.config import get_config


def send_alert_email(subject: str, body: str) -> bool:
    """Best-effort: never raises. Alerting must not itself crash (or mask
    the exit code of) whatever it's reporting on. Returns whether `mail`
    accepted the message."""
    to_addr = get_config().alert_email_to
    if not to_addr:
        print(f"ALERT_EMAIL_TO not configured, alert not sent:\n{subject}\n{body}", file=sys.stderr)
        return False
    return send_email(to_addr, subject, body)


# `mail` hands off to the local Postfix queue, so failures are rare and
# short-lived (Postfix restarting, a slow disk); three tries over ~45s.
MAIL_ATTEMPTS = 3
MAIL_RETRY_DELAYS_SECONDS = (15.0, 30.0)


def send_email(
    to_addr: str,
    subject: str,
    body: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    run_fn: Callable[..., object] = subprocess.run,
) -> bool:
    """Send via the local `mail` command, retrying a failed hand-off. Never
    raises; returns whether `mail` accepted the message."""
    last_exc: Exception | None = None
    for attempt in range(MAIL_ATTEMPTS):
        try:
            run_fn(
                ["mail", "-s", subject, to_addr],
                input=body,
                text=True,
                timeout=30,
                check=True,
            )
            return True
        except FileNotFoundError as exc:  # no `mail` binary: retrying can't help
            last_exc = exc
            break
        except Exception as exc:  # noqa: BLE001 -- alerting must degrade to a log line, never propagate
            last_exc = exc
            if attempt < MAIL_ATTEMPTS - 1:
                delay = MAIL_RETRY_DELAYS_SECONDS[min(attempt, len(MAIL_RETRY_DELAYS_SECONDS) - 1)]
                print(f"mail attempt {attempt + 1}/{MAIL_ATTEMPTS} failed ({exc}); retrying in {delay:.0f}s", file=sys.stderr)
                sleep_fn(delay)
    print(f"Failed to send email to {to_addr} ({last_exc}):\n{subject}\n{body}", file=sys.stderr)
    return False
