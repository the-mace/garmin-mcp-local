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

## Repo specifics

- The default branch is `master`, not `main`.
- After a pull that touches `pyproject.toml`:
  `source .venv/bin/activate && pip install -e ".[dev]"`.
- Tests: `pytest` (idempotency, rate limiter, monitoring). Setup, launchd jobs,
  and architecture are in `README.md`.
