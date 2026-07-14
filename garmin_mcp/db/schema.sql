-- Garmin Connect local cache schema.
--
-- Design notes:
--   * Every table has a stable natural key from Garmin's own IDs
--     (activity_id, calendar_date) with a UNIQUE/PRIMARY KEY constraint,
--     so re-running sync or import is idempotent (INSERT ... ON CONFLICT
--     DO UPDATE, never a plain INSERT).
--   * `source` columns record whether a row came from the bulk export
--     ('csv_export' -- historically CSV, now Garmin ships JSON) or a live
--     API call ('api'), so it's always clear what's backfilled vs. fresh.
--   * Repeating substructures (laps, HR/power zones, stress period
--     breakdowns) get their own child table rather than being flattened
--     or dumped as JSON, so they stay queryable.
--   * A few genuinely free-form/rare nested structures (strength-training
--     exercise sets, dive info, raw lap measurement arrays) are kept as
--     JSON columns rather than being force-normalized into sparse tables
--     that would mostly be NULL for the other 99% of activity types.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Activities
-- ============================================================

CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    uuid TEXT,
    name TEXT,
    activity_type TEXT,
    sport_type TEXT,
    event_type_id INTEGER,
    begin_timestamp_utc_ms INTEGER NOT NULL,
    begin_timestamp_local_ms INTEGER,
    timezone_id INTEGER,
    duration_s REAL,
    elapsed_duration_s REAL,
    moving_duration_s REAL,
    distance_m REAL,
    calories REAL,
    avg_hr REAL,
    max_hr REAL,
    min_hr REAL,
    avg_speed_mps REAL,
    max_speed_mps REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    min_elevation_m REAL,
    max_elevation_m REAL,
    avg_power_w REAL,
    max_power_w REAL,
    norm_power_w REAL,
    max_20min_power_w REAL,
    avg_bike_cadence REAL,
    max_bike_cadence REAL,
    avg_run_cadence REAL,
    max_run_cadence REAL,
    avg_double_cadence REAL,
    max_double_cadence REAL,
    avg_swim_cadence REAL,
    avg_stroke_distance REAL,
    avg_strokes REAL,
    avg_swolf REAL,
    strokes REAL,
    pool_length REAL,
    steps INTEGER,
    total_reps INTEGER,
    total_sets INTEGER,
    training_stress_score REAL,
    intensity_factor REAL,
    aerobic_training_effect REAL,
    aerobic_training_effect_message TEXT,
    anaerobic_training_effect REAL,
    anaerobic_training_effect_message TEXT,
    training_effect_label TEXT,
    activity_training_load REAL,
    vo2max_value REAL,
    avg_respiration_rate REAL,
    max_respiration_rate REAL,
    min_respiration_rate REAL,
    avg_stress REAL,
    start_stress REAL,
    end_stress REAL,
    difference_stress REAL,
    difference_body_battery REAL,
    avg_vertical_oscillation REAL,
    avg_vertical_ratio REAL,
    avg_ground_contact_time REAL,
    avg_vertical_speed REAL,
    max_vertical_speed REAL,
    avg_grade_adjusted_speed REAL,
    device_id INTEGER,
    manufacturer TEXT,
    course_id INTEGER,
    workout_id INTEGER,
    workout_feel INTEGER,
    workout_rpe INTEGER,
    location_name TEXT,
    description TEXT,
    start_latitude REAL,
    start_longitude REAL,
    end_latitude REAL,
    end_longitude REAL,
    min_latitude REAL,
    min_longitude REAL,
    max_latitude REAL,
    max_longitude REAL,
    lap_count INTEGER,
    favorite INTEGER,
    pr INTEGER,
    purposeful INTEGER,
    elevation_corrected INTEGER,
    water_estimated REAL,
    max_temperature REAL,
    min_temperature REAL,
    moderate_intensity_minutes INTEGER,
    vigorous_intensity_minutes INTEGER,
    exercise_sets_json TEXT,   -- strength training: summarizedExerciseSets
    dive_info_json TEXT,       -- diving: summarizedDiveInfo
    split_summaries_json TEXT, -- coarse split rollups distinct from per-lap detail
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activities_begin_ts ON activities(begin_timestamp_utc_ms);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(activity_type);

-- Per-lap ("split" in Garmin's export) detail, not just activity summaries.
CREATE TABLE IF NOT EXISTS activity_laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
    lap_index INTEGER NOT NULL,
    start_time_utc_ms INTEGER,
    end_time_utc_ms INTEGER,
    distance_m REAL,
    duration_s REAL,
    avg_hr REAL,
    max_hr REAL,
    avg_power_w REAL,
    max_power_w REAL,
    avg_speed_mps REAL,
    max_speed_mps REAL,
    avg_cadence REAL,
    max_cadence REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    calories REAL,
    measurements_json TEXT,
    UNIQUE (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS activity_hr_zones (
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
    zone_number INTEGER NOT NULL,
    seconds_in_zone REAL,
    PRIMARY KEY (activity_id, zone_number)
);

CREATE TABLE IF NOT EXISTS activity_power_zones (
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
    zone_number INTEGER NOT NULL,
    seconds_in_zone REAL,
    PRIMARY KEY (activity_id, zone_number)
);

CREATE TABLE IF NOT EXISTS gear (
    gear_id INTEGER PRIMARY KEY,
    uuid TEXT,
    gear_type TEXT,
    status TEXT,
    make_model TEXT,
    date_begin TEXT,
    date_end TEXT,
    max_distance_m REAL,
    source TEXT NOT NULL DEFAULT 'csv_export',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_gear (
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
    gear_id INTEGER NOT NULL REFERENCES gear(gear_id) ON DELETE CASCADE,
    PRIMARY KEY (activity_id, gear_id)
);

-- ============================================================
-- Daily health metrics
-- ============================================================

-- One row per calendar day: steps/calories/floors/HR/stress/body
-- battery/SpO2/respiration summary (Garmin's "User Daily Summary").
CREATE TABLE IF NOT EXISTS daily_health_metrics (
    calendar_date TEXT PRIMARY KEY,  -- 'YYYY-MM-DD'
    total_steps INTEGER,
    daily_step_goal INTEGER,
    total_distance_m REAL,
    total_calories REAL,
    active_calories REAL,
    bmr_calories REAL,
    floors_ascended_m REAL,
    floors_descended_m REAL,
    floors_ascended_goal REAL,
    intensity_minutes_moderate INTEGER,
    intensity_minutes_vigorous INTEGER,
    intensity_minutes_goal INTEGER,
    resting_hr INTEGER,
    current_day_resting_hr INTEGER,
    min_hr INTEGER,
    max_hr INTEGER,
    avg_stress INTEGER,
    max_stress INTEGER,
    stress_duration_s INTEGER,
    rest_stress_duration_s INTEGER,
    activity_stress_duration_s INTEGER,
    low_stress_duration_s INTEGER,
    medium_stress_duration_s INTEGER,
    high_stress_duration_s INTEGER,
    body_battery_charged INTEGER,
    body_battery_drained INTEGER,
    body_battery_highest INTEGER,
    body_battery_lowest INTEGER,
    body_battery_most_recent INTEGER,
    avg_spo2 REAL,
    latest_spo2 REAL,
    lowest_spo2 REAL,
    avg_waking_respiration REAL,
    highest_respiration REAL,
    lowest_respiration REAL,
    latest_respiration REAL,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Stress breakdown by period (TOTAL/AWAKE/ASLEEP) -- a genuine repeating
-- group in Garmin's export, not force-flattened into daily_health_metrics.
CREATE TABLE IF NOT EXISTS daily_stress_periods (
    calendar_date TEXT NOT NULL REFERENCES daily_health_metrics(calendar_date) ON DELETE CASCADE,
    period_type TEXT NOT NULL,  -- TOTAL | AWAKE | ASLEEP
    avg_stress_level REAL,
    max_stress_level REAL,
    stress_duration_s INTEGER,
    rest_duration_s INTEGER,
    activity_duration_s INTEGER,
    low_duration_s INTEGER,
    medium_duration_s INTEGER,
    high_duration_s INTEGER,
    PRIMARY KEY (calendar_date, period_type)
);

CREATE TABLE IF NOT EXISTS sleep (
    calendar_date TEXT PRIMARY KEY,
    sleep_start_utc_ms INTEGER,
    sleep_end_utc_ms INTEGER,
    deep_sleep_s INTEGER,
    light_sleep_s INTEGER,
    rem_sleep_s INTEGER,
    awake_s INTEGER,
    unmeasurable_s INTEGER,
    awake_count INTEGER,
    restless_moment_count INTEGER,
    avg_respiration REAL,
    avg_sleep_stress REAL,
    overall_score INTEGER,
    quality_score INTEGER,
    duration_score INTEGER,
    recovery_score INTEGER,
    deep_score INTEGER,
    rem_score INTEGER,
    light_score INTEGER,
    awakenings_count_score INTEGER,
    awake_time_score INTEGER,
    restfulness_score INTEGER,
    interruptions_score INTEGER,
    combined_awake_score INTEGER,
    feedback TEXT,
    insight TEXT,
    sleep_window_confirmation_type TEXT,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Nightly HRV. NOTE: Garmin's bulk export does not include a dedicated
-- nightly-HRV file for every account -- only a weekly average surfaces
-- indirectly via TrainingReadinessDTO. last_night_avg/baseline/status are
-- populated by the live API (get_hrv_data) during sync, not CSV import.
CREATE TABLE IF NOT EXISTS hrv_daily (
    calendar_date TEXT PRIMARY KEY,
    weekly_avg REAL,
    last_night_avg REAL,
    last_night_5min_high REAL,
    baseline_balanced_low REAL,
    baseline_balanced_upper REAL,
    status TEXT,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Body composition (Garmin Index smart scale). Left empty by import for
-- accounts with no scale data in their export -- see bulk_import logs.
CREATE TABLE IF NOT EXISTS body_composition (
    calendar_date TEXT PRIMARY KEY,
    measurement_timestamp_utc_ms INTEGER,
    weight_g REAL,
    bmi REAL,
    body_fat_percent REAL,
    body_water_percent REAL,
    bone_mass_g REAL,
    muscle_mass_g REAL,
    visceral_fat_rating REAL,
    metabolic_age REAL,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Training metrics
-- ============================================================

CREATE TABLE IF NOT EXISTS training_status (
    calendar_date TEXT PRIMARY KEY,
    training_status TEXT,
    fitness_trend TEXT,
    vo2max_running REAL,
    vo2max_cycling REAL,
    acute_load REAL,
    chronic_load REAL,
    acwr_percent REAL,
    acwr_status TEXT,
    endurance_score REAL,
    hill_strength_score REAL,
    hill_endurance_score REAL,
    hill_overall_score REAL,
    hill_classification_id INTEGER,
    max_met REAL,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS training_readiness (
    calendar_date TEXT PRIMARY KEY,
    score INTEGER,
    level TEXT,
    feedback_long TEXT,
    feedback_short TEXT,
    sleep_score INTEGER,
    sleep_score_factor_percent INTEGER,
    recovery_time_minutes INTEGER,
    recovery_time_factor_percent INTEGER,
    acwr_factor_percent INTEGER,
    stress_history_factor_percent INTEGER,
    hrv_factor_percent INTEGER,
    hrv_weekly_average REAL,
    sleep_history_factor_percent INTEGER,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS race_predictions (
    calendar_date TEXT PRIMARY KEY,
    time_5k_s REAL,
    time_10k_s REAL,
    time_half_marathon_s REAL,
    time_marathon_s REAL,
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Sync state
-- ============================================================

-- Full audit trail of every sync/backfill/import attempt, success or not.
-- This is what makes truncated/rate-limited runs visible after the fact.
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    run_type TEXT NOT NULL,           -- 'bulk_import' | 'incremental_sync' | 'backfill'
    cursor_type TEXT,                 -- 'date' | 'activity_id'
    range_start TEXT,
    range_end TEXT,
    last_activity_id INTEGER,
    status TEXT NOT NULL,             -- 'success' | 'partial' | 'failed' | 'rate_limited'
    records_expected INTEGER,
    records_fetched INTEGER,
    warning TEXT,
    error_message TEXT,
    backoff_until_utc_ms INTEGER,     -- persisted cooldown so the next run doesn't immediately re-hammer the API
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_log_category ON sync_log(category, started_at);

-- Current resume point per category/direction, derived from sync_log but
-- kept as its own small table so resuming doesn't require scanning history.
-- 'forward' = newest data reached by incremental sync (moves toward today).
-- 'backward' = oldest data reached by backfill (moves toward account creation).
CREATE TABLE IF NOT EXISTS sync_cursor (
    category TEXT NOT NULL,
    direction TEXT NOT NULL,          -- 'forward' | 'backward'
    cursor_type TEXT NOT NULL,        -- 'date' | 'activity_id'
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (category, direction)
);
