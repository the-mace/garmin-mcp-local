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

from garmin_mcp.config import get_config


def send_alert_email(subject: str, body: str) -> bool:
    """Best-effort: never raises. Alerting must not itself crash (or mask
    the exit code of) whatever it's reporting on. Returns whether `mail`
    accepted the message."""
    to_addr = get_config().alert_email_to
    if not to_addr:
        print(f"ALERT_EMAIL_TO not configured, alert not sent:\n{subject}\n{body}", file=sys.stderr)
        return False
    try:
        subprocess.run(
            ["mail", "-s", subject, to_addr],
            input=body,
            text=True,
            timeout=30,
            check=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- alerting must degrade to a log line, never propagate
        print(f"Failed to send alert email ({exc}):\n{subject}\n{body}", file=sys.stderr)
        return False
