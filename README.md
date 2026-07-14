# garmin-mcp-local

A personal Garmin Connect MCP server: a local SQLite cache of your Garmin
data, a resumable historical backfill (from Garmin's bulk data export and/or
the live API), and a rate-limited sync so nothing gets silently dropped or
duplicated. Exposes read-only MCP tools backed entirely by the local cache,
plus one explicit `sync` tool that talks to the live API.

Built on [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
(cyberjunky) as the only Garmin API dependency.

## Why a local cache instead of calling the API every time?

Garmin Connect's API is not designed for bulk historical queries and will
rate-limit (429) or block (403) you if you hit it too hard. This project
fetches once, caches locally in SQLite, and answers all normal queries from
that cache. A live sync is a separate, explicit step.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# edit .env: set GARMIN_EMAIL / GARMIN_PASSWORD (only needed for the first
# login -- after that, the session token is cached under GARMIN_TOKEN_STORE
# and credentials aren't read again unless the token expires/is revoked)
```

Nothing in `.env` is ever committed -- `.gitignore` excludes it, the SQLite
DB file, and the token cache directory. `.env.example` only has placeholders.

## First-run backfill

### 1. Seed from Garmin's bulk data export (recommended -- avoids re-fetching years of history through the rate-limited API)

Request your data from Garmin: **Garmin Connect → Account Settings → Export
Your Data**. Garmin emails you a download link for a zip archive (despite
often being called a "CSV export" request, the archive Garmin actually
ships is JSON files organized under `DI_CONNECT/` by category -- this
project parses that directly, no CSV involved).

```bash
garmin-mcp-import-export /path/to/your-export.zip
```

This prints a report of what was imported per category, and explicitly
flags what the export *can't* populate (see [Known gaps](#known-gaps-in-the-bulk-export)
below). Re-running the import against the same (or an updated) export file
is always safe -- every table has a natural-key `UNIQUE` constraint and the
importer upserts, so nothing is ever duplicated.

### 2. Fill in anything the export didn't cover, via the live API

```bash
garmin-mcp-backfill --batch-days 30 --earliest-date 2015-01-01
```

Each invocation walks one controlled batch further back into history per
category (rather than trying to fetch everything at once) and persists a
resume cursor, so you can run it repeatedly (e.g. on a cron job, or via
the MCP `backfill_batch_now` tool) until it reaches `--earliest-date`.

## Ongoing incremental sync

```bash
garmin-mcp-sync
```

Pulls everything new since the last successful sync, for every category.
This is **never** triggered automatically by a query -- run it explicitly,
or wire it into a cron job / the MCP `sync_now` tool.

Every API call in this project goes through one rate-limited wrapper
(`garmin_mcp/garmin_client/rate_limiter.py`) that:
- enforces a minimum delay between requests (`GARMIN_MIN_REQUEST_INTERVAL_SECONDS`),
- retries 429/403 with exponential backoff + jitter, capped at `GARMIN_MAX_RETRIES`,
- persists the backoff/cooldown window to `sync_log`, so if a run gets
  rate-limited and dies, the *next* run (even a fresh process) sees the
  still-active cooldown and refuses to hammer the API again until it expires,
- logs a clear failure (not a silent give-up) once retries are exhausted.

Truncation is never silent: every batch fetch records `records_expected`
vs. `records_fetched` in `sync_log`, and anything short of a full,
unambiguous fetch is marked `status='partial'` with a specific warning
(e.g. which dates failed, or which paginated batch came back short without
an explicit end-of-data signal from the API).

Check `SELECT * FROM sync_log ORDER BY started_at DESC LIMIT 20` (or the
MCP `get_sync_status` tool) any time you want to know what's actually been
fetched vs. what's stale or failed.

## Scheduling (launchd)

`sync` and `backfill` only run when invoked -- to run them automatically,
three `launchd` LaunchAgents are provided under `launchd/`:

- `local.garmin-mcp.sync` -- daily at 06:00, incremental catch-up.
- `local.garmin-mcp.backfill` -- daily at 06:30 (staggered 30 min after
  sync so the two never write to the SQLite DB concurrently), 30-day
  batches, scoped to the 7 wellness/health categories (`activities` is
  excluded by default since a CSV-export seed already covers full activity
  history -- edit the plist's `--category` flags to include it if you
  didn't seed from a CSV export).
- `local.garmin-mcp.watchdog` -- daily at 08:00 and 20:00, catches a
  scheduled run that failed silently (see "Failure alerting" below).

`launchd` requires literal absolute paths (no `~` expansion, no env vars),
so the plists in `launchd/` are templates with a `__REPO_ROOT__`
placeholder -- install generates the real plists (with your actual path
baked in) directly into `~/Library/LaunchAgents/`, rather than symlinking
the template in place:

```bash
REPO_ROOT="$(pwd)"
for job in sync backfill watchdog; do
    sed "s|__REPO_ROOT__|$REPO_ROOT|g" \
        "launchd/local.garmin-mcp.$job.plist" \
        > ~/Library/LaunchAgents/"local.garmin-mcp.$job.plist"
done
launchctl load ~/Library/LaunchAgents/local.garmin-mcp.sync.plist
launchctl load ~/Library/LaunchAgents/local.garmin-mcp.backfill.plist
launchctl load ~/Library/LaunchAgents/local.garmin-mcp.watchdog.plist
```

Logs land in `logs/sync.log` / `logs/sync.error.log` (and the `backfill`
and `watchdog` equivalents). Trigger a run immediately (e.g. to test) with:

```bash
launchctl start local.garmin-mcp.sync
```

Because the generated files in `~/Library/LaunchAgents/` are a one-time
copy (not a symlink back to the template), re-run the `sed` step above and
`launchctl unload`/`load` again after editing a template in `launchd/` for
the change to take effect.

### Failure alerting

A scheduled job can fail silently two different ways: it can crash (or
never run at all -- disabled agent, sleeping laptop at 06:00), or it can
exit 0 while a per-date fetch failed internally and got logged to
`sync_log` as `status='partial'`/`'failed'`/`'rate_limited'` without
raising (see `garmin_mcp/sync/engine.py` -- one bad date is swallowed
rather than aborting the whole batch). Neither shows up unless something
reads the logs.

Set `ALERT_EMAIL_TO` in `.env` to get an email for either case, sent via
the local `mail` command (this machine already relays outbound mail
through Postfix to a real SMTP provider -- confirm `echo test | mail -s
test you@example.com` reaches your inbox before relying on this). This is
deliberately *not* a hosted heartbeat/uptime service: nothing pings out on
a normal successful day, so there's no regular outbound signal revealing
when this machine is online. The only network traffic this ever generates
is the alert itself, sent only when there's actually something to report.

- `garmin-mcp-sync` / `garmin-mcp-backfill` email immediately on any
  unhandled exception (login failure, DB error, etc.), then re-raise --
  the real exit code and traceback still land in `logs/*.error.log`
  exactly as before, alerting never masks it.
- `garmin-mcp-watchdog` (the third launchd job above) is what catches the
  other two failure modes -- a job that never ran, or one that ran but
  logged `partial`/`failed`/`rate_limited`. It reads `sync_log` for the
  most recent run per category and per `run_type`, and emails one summary
  if anything's stale (>27h old, one day of slack over the daily cadence)
  or unsuccessful. It sends nothing when everything's healthy.

Leaving `ALERT_EMAIL_TO` unset disables alerting entirely (failures are
still visible in `logs/*.error.log` and `sync_log`, just not pushed to
you). Note the watchdog job has the same "did launchd even run this"
blind spot as the jobs it's watching -- there's no way to fully close that
loop with a local-only design, which is the tradeoff for not running a
regular external heartbeat. Scheduling it twice a day (08:00 and 20:00)
mitigates but doesn't eliminate this.

### macOS Documents-folder permission (if the project lives under `~/Documents`)

If your project sits under `~/Documents` (or Desktop/Downloads), macOS's
TCC privacy protection blocks `launchd`-spawned processes from reading
files there -- including the venv's own `pyvenv.cfg` -- **unless** the
Python interpreter has an explicit grant. This will surface as:

```
PermissionError: [Errno 1] Operation not permitted: '.../.venv/pyvenv.cfg'
```

This is genuinely fiddly to resolve, because System Settings' Full Disk
Access / Files and Folders "+" picker generally only lets you select `.app`
bundles, not a raw CLI Python binary -- and a background `launchd` job has
no GUI session to show an interactive consent prompt in the first place, so
it just fails silently rather than asking. What actually works:

1. Find the *real* interpreter binary your venv resolves to (pyenv/asdf
   installs are usually a symlink chain):
   ```bash
   python3 -c "import os; print(os.path.realpath('.venv/bin/python3'))"
   ```
2. Build a minimal `.app` bundle wrapper around it (a real bundle *is*
   selectable in the TCC picker, and launching it from Finder -- with no
   privileged GUI-app ancestor to silently inherit permission from -- gives
   it a chance to earn its own independent, persisted grant via a genuine
   consent dialog). Keep the wrapper narrowly scoped to the specific
   command(s) you need, not a generic pass-through that can exec anything
   it's handed.
3. Double-click that `.app` once in Finder. If a system permission dialog
   appears, click Allow.
4. Verify the grant landed on the *interpreter*, not the wrapper app:
   ```bash
   sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" \
     "SELECT service, client, auth_value FROM access WHERE client LIKE '%python3.12%';"
   ```
   `auth_value=2` for `kTCCServiceSystemPolicyDocumentsFolder` means it
   worked. The grant is tied to that interpreter binary's identity, not to
   how it's invoked -- so once this is confirmed, `launchd` can call the
   venv binaries **directly** (no wrapper needed going forward; the plists
   in this repo already do this).
5. If `launchd`'s job also sets `WorkingDirectory` to a path under
   Documents, drop it -- `/bin/bash` (or whatever shell interprets the
   job) has no grant of its own, and `launchd` `chdir()`-ing it into a
   protected folder before your granted interpreter ever runs breaks the
   shell's own `getcwd()` at startup. This project's config
   (`garmin_mcp/config.py`) resolves `.env` and all paths relative to the
   repo root explicitly, independent of `cwd`, specifically so
   `WorkingDirectory` isn't needed.

## MCP server (Claude Desktop / Claude Code)

```bash
garmin-mcp-server   # runs on stdio
```

Add to Claude Desktop's config (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS) under `mcpServers`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/absolute/path/to/garmin-mcp-local/.venv/bin/garmin-mcp-server"
    }
  }
}
```

No `env` overrides needed -- `garmin_mcp/config.py` resolves `.env`, the DB
path, and the token store relative to the repo root explicitly, independent
of whatever working directory Claude Desktop launches the process with.
Restart Claude Desktop to pick up the change.

Tools exposed:

| Tool | Hits the network? | Purpose |
|---|---|---|
| `list_activities` | No | Activities by date range / type |
| `get_activity_detail` | No | Full detail: laps, HR/power zones, gear |
| `get_daily_health_metrics` | No | Steps/HR/stress/body battery/SpO2/respiration by date range |
| `get_sleep` | No | Nightly sleep stages + score breakdown |
| `get_training_trends` | No | Training status/readiness/VO2max/load/race predictions |
| `get_sync_status` | No | Recent sync_log entries + resume cursors |
| `execute_sql` | No | Ad hoc read-only SQL (`SELECT`/`WITH`/`EXPLAIN`/`PRAGMA table_info` only) |
| `sync_now` | **Yes** | Explicit incremental sync |
| `backfill_batch_now` | **Yes** | One controlled backfill batch per category |

## Schema

One normalized table per data category (see `garmin_mcp/db/schema.sql` for
the authoritative, commented definition):

- **Activities**: `activities` (full per-activity detail), `activity_laps`,
  `activity_hr_zones`, `activity_power_zones`, `gear`, `activity_gear`.
- **Daily health**: `daily_health_metrics` (steps/calories/HR/stress/body
  battery/SpO2/respiration), `daily_stress_periods` (TOTAL/AWAKE/ASLEEP
  breakdown), `sleep`, `hrv_daily`, `body_composition`.
- **Training metrics**: `training_status` (VO2max, training status, load/ACWR,
  endurance & hill scores), `training_readiness`, `race_predictions`.
- **Sync state**: `sync_log` (full audit trail of every import/sync/backfill
  attempt, success or not) and `sync_cursor` (current resume point per
  category, in both the forward/incremental and backward/backfill direction).

Every table has a stable natural key from Garmin's own IDs (`activity_id`,
`calendar_date`) with a `UNIQUE`/`PRIMARY KEY` constraint. All writes go
through one generic `upsert()` helper (`garmin_mcp/db/connection.py`), so
re-running any import or sync is always idempotent.

## Known gaps in the bulk export

Verified against a real Garmin "export all data" archive. The importer's
report flags these explicitly at the end of every `garmin-mcp-import-export`
run:

- **Nightly HRV detail** (`hrv_daily.last_night_avg`, baseline, status):
  the bulk export has no dedicated nightly-HRV file. Only a weekly average
  is derivable indirectly (from `TrainingReadinessDTO`), imported with
  `source='csv_export_approx'`. Run a live sync (`get_hrv_data`) to backfill
  nightly detail.
- **Body composition** (`body_composition`): only populated if your account
  has Garmin Index smart scale data in the export. If you don't own one,
  this table stays empty until/unless you add manual weigh-ins via the API.
- **Raw GPS tracks / second-by-second streams**: intentionally out of scope
  for this schema (per-lap summaries and HR/power zone time-in-zone are
  captured instead) -- keeps the DB small and avoids one API call per
  activity during backfill. The original FIT files are included separately
  in Garmin's export archive (`DI_CONNECT/DI-Connect-Uploaded-Files/`) if
  you need them.

### A note on units in the bulk export

Garmin's raw export uses internal units for activity-level and per-lap
fields that don't match their own field-name suffixes: distance-like
fields are centimeters, speed-like fields are centimeters/millisecond,
duration-like fields are milliseconds. This was verified empirically
against real ride/run data during development (see comments in
`garmin_mcp/bulk_import/units.py`) and converted to meters/mps/seconds on
import. A few running-dynamics fields (vertical oscillation, ground
contact time, vertical ratio, cadence) are left as raw export values,
since their exact unit wasn't independently verifiable from the sample
data available -- cross-check against Garmin Connect's UI before relying
on them for anything precise.

### A note on live-API field mappings

Both the bulk-export importer and the live-API sync mappings
(`garmin_mcp/sync/daily_categories.py`, `garmin_mcp/sync/activities_sync.py`)
have been verified against a real account -- including a direct
field-by-field diff between CSV-imported and freshly API-backfilled data
for the same historical date. A few real discrepancies from that process
are now documented inline in the code and worth knowing about:

- `get_user_summary` (the live daily-health endpoint) returns a **flat**
  structure -- unlike the bulk export's nested `allDayStress`/`bodyBattery`/
  `respiration` objects, and with several different field names
  (`averageStressLevel` vs. nested `allDayStress.aggregatorList[TOTAL]`,
  `bodyBatteryHighestValue` vs. a `bodyBatteryStatList` lookup, etc.).
  Once mapped correctly, values match the CSV export exactly for the same
  date.
- `daily_stress_periods`' AWAKE/ASLEEP breakdown is CSV-only -- the live
  endpoint only exposes a TOTAL-equivalent, so live sync writes just that
  row and leaves any existing CSV-sourced AWAKE/ASLEEP rows alone.
- `training_status`/`fitness_trend` are numeric codes live (`7`, `1`, ...),
  not the plain strings the bulk export gives (`"MAINTAINING"`,
  `"DECREASING"`) -- there's no public code-to-string mapping, so expect
  `source='api'` rows to look different in flavor from `source='csv_export'`
  rows in this column specifically.
- `sleep`'s per-stage quality subscores (`deep_score`, `rem_score`, etc.)
  and `restless_moment_count` aren't available from the live endpoint at
  all (only an overall score, feedback string, and raw stage percentages)
  -- left `NULL` on `source='api'` rows rather than approximated.
- `body_composition` and `hrv_daily` (nightly detail) fill in correctly
  from live sync where the CSV export couldn't provide them.

If you spot a live-synced row that looks wrong, `SELECT ... WHERE
source='api'` vs. `source='csv_export'` for the same date is the fastest
way to compare and confirm before assuming it's a bug.

## Tests

```bash
pytest
```

Focused specifically on the two failure modes this project exists to avoid:

- `tests/test_idempotency.py`: re-running an import or sync never
  duplicates rows; a partially-failed sync batch resumes from the right
  date instead of silently skipping the gap or re-processing everything.
- `tests/test_rate_limiter.py`: exponential backoff math, persisted
  cooldown blocking a fresh process from hammering the API again, and
  that retries are capped with a clear logged failure rather than an
  infinite loop or a silent give-up.
