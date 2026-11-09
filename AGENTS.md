# Agent instructions: garmin-mcp-local

Shared rules (pull before work, tests and lint, secrets, commit attribution)
live in the global `~/.config/agents/AGENTS.md`. If that file is not loaded
(cloud or CI sessions), the short version is: `git pull --ff-only` first, run
the tests below before committing, never commit secrets, and end agent commits
with a `Co-Authored-By: <Tool> (<model>) <email>` trailer.

## Which coding agent

Use Claude Code with Sonnet for small, contained fixes driven by a watchdog or
sync alert email. Use Opus 5.5 for multi-file changes to the sync engine,
schema, or rate limiter. Grok Build is the second opinion.

## External actions must retry

Every external action (HTTP call, login, subprocess that talks to a
service, mail) needs a bounded retry with backoff for transient failures
(5xx, timeouts, connection errors), and must not retry failures a retry
can't fix (4xx, bad credentials, MFA). This applies to every repo, not
just this one. Here, use `garmin_mcp/retry.py` (`is_transient`,
`retry_call`, `run_with_retries`) rather than writing a new loop.

## Repo specifics

- The default branch is `master`, not `main`.
- After a pull that touches `pyproject.toml`:
  `source .venv/bin/activate && pip install -e ".[dev]"`.
- Tests: `pytest` (idempotency, rate limiter, monitoring, official API and FIT
  laps, daily summary, evening activities sync, retries). Setup, launchd jobs, and
  architecture are in `README.md`.
- Two Garmin clients: the official Personal API
  (`garmin_client/personal_api.py`) and unofficial `garminconnect`, bundled in
  `GarminClients` (`garmin_client/factory.py`). Category routing lives in
  `sync/daily_categories.py` (`_official_first` / `_garminconnect_only`).

## Retry layers (added after the 2026-11-09 sync failure)

The 08:00 sync on 2026-11-09 crashed on one 504 from the Personal API token
endpoint: the rate limiter only retried 429/403, and an activities page
error aborted the whole run. Now:

- `RateLimitedClient.call` retries transient errors (no persisted
  cooldown) and fails fast once Garmin looks down.
- Activities page failures are recorded as `partial` with
  `fetch_failed`, never a crash.
- `build_client` retries transient login failures only.
- `scripts/sync.py` and `scripts/activities_sync.py` wrap the run in
  `run_with_retries` driven by `sync_needs_retry` (transient failures only).
- `alerting.send_email` retries the `mail` hand-off.
