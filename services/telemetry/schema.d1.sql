-- D1 (SQLite) schema for the FreeRide telemetry receiver.
-- Apply with:
--   wrangler d1 execute freeride-telemetry --remote --file=./schema.d1.sql
--
-- Storage history: D1 (May 2026) -> Neon Postgres (2026-05-28) -> back to
-- D1 (2026-10-07). Neon billed compute-hours and the hourly beacons plus
-- the 5-minute stats poll kept its compute awake 24/7. D1 bills by rows
-- scanned instead, so this schema keeps write-time rollups next to the raw
-- beacon log: /v1/stats never scans the beacons table.
--
-- schema.pg.sql is kept as the frozen reference of the Neon shape; the
-- Neon projects themselves are left intact as a read-only backup.

-- ─── beacons ────────────────────────────────────────────────────
-- One row per heartbeat from a running freeride install (raw log).
-- Written by POST /v1/beacon. NEVER stores IPs / hostnames.
CREATE TABLE IF NOT EXISTS beacons (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  installation_id   TEXT NOT NULL,
  version           TEXT,
  os                TEXT,
  -- Cumulative lifetime counters as reported by the install. Old
  -- gateways only ship tokens_served; new ones ship the split fields
  -- and tokens_served = input + output.
  tokens_served     INTEGER NOT NULL DEFAULT 0,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  request_count     INTEGER NOT NULL DEFAULT 0,
  providers_active  TEXT,                          -- JSON array
  uptime_hours      INTEGER NOT NULL DEFAULT 0,
  received_at       INTEGER NOT NULL,              -- unix epoch seconds
  -- 0 until the row has been folded into install_state / hourly_totals.
  -- The worker inserts with 0 and sets 1 in the same batch as the
  -- rollup, so repair_rollups.sql can find rows whose rollup step failed
  -- (D1 over its read budget, for example) without guessing from
  -- watermarks. Existing rows default to 1.
  rolled            INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_beacons_unrolled
  ON beacons(rolled) WHERE rolled = 0;

CREATE INDEX IF NOT EXISTS idx_beacons_received_at
  ON beacons(received_at);

CREATE INDEX IF NOT EXISTS idx_beacons_installation_id
  ON beacons(installation_id);

-- Natural key: the worker stamps one received_at per beacon. Beacons
-- are hourly per install, so same-second collisions do not occur.
CREATE UNIQUE INDEX IF NOT EXISTS uq_beacons_install_received
  ON beacons(installation_id, received_at);


-- ─── install_state ──────────────────────────────────────────────
-- One row per installation, maintained on every beacon write: the
-- last reported cumulative counters plus the reset-aware delta-sum
-- accumulated over the install's lifetime. A counter that DROPS
-- below the previous value is a reset (reinstall, cleared
-- stats.json): the full new value counts as freshly served.
--
-- total.* on /v1/stats is SUM(acc_*) over this table (hundreds of
-- rows), which is exactly what the old LAG() window query over the
-- whole beacons table computed.
CREATE TABLE IF NOT EXISTS install_state (
  installation_id     TEXT PRIMARY KEY,
  version             TEXT,
  os                  TEXT,
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  last_tokens_served  INTEGER NOT NULL DEFAULT 0,
  last_input_tokens   INTEGER NOT NULL DEFAULT 0,
  last_output_tokens  INTEGER NOT NULL DEFAULT 0,
  last_request_count  INTEGER NOT NULL DEFAULT 0,
  acc_tokens_served   INTEGER NOT NULL DEFAULT 0,
  acc_input_tokens    INTEGER NOT NULL DEFAULT 0,
  acc_output_tokens   INTEGER NOT NULL DEFAULT 0,
  acc_request_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_install_state_last_seen
  ON install_state(last_seen);


-- ─── hourly_totals ──────────────────────────────────────────────
-- Delta-sum per wall-clock hour (hour = received_at - received_at % 3600).
-- last_24h.* and the trailing-7d rate read 24 / 168 rows from here.
CREATE TABLE IF NOT EXISTS hourly_totals (
  hour              INTEGER PRIMARY KEY,
  tokens_served     INTEGER NOT NULL DEFAULT 0,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  request_count     INTEGER NOT NULL DEFAULT 0,
  beacons           INTEGER NOT NULL DEFAULT 0
);


-- ─── openrouter_aggregate ───────────────────────────────────────
-- Per-fetch snapshot of OpenRouter app-level token totals for V1 +
-- V3 apps. Filled by the hourly cron scraper.
CREATE TABLE IF NOT EXISTS openrouter_aggregate (
  fetched_at        INTEGER PRIMARY KEY,
  v1_tokens         INTEGER NOT NULL,
  v3_tokens         INTEGER NOT NULL,
  combined_tokens   INTEGER NOT NULL
);


-- ─── openrouter_daily ───────────────────────────────────────────
-- Per-day per-model breakdown from the app page's `daily` series: the
-- trailing ~30 days, whose sum equals the page's totalTokens. Rows
-- older than the window are kept as history but are not re-scraped.
CREATE TABLE IF NOT EXISTS openrouter_daily (
  date              TEXT NOT NULL,
  app               TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tokens            INTEGER NOT NULL,
  scraped_at        INTEGER NOT NULL,
  PRIMARY KEY (date, app, model_id)
);

CREATE INDEX IF NOT EXISTS idx_or_daily_date
  ON openrouter_daily(date);

CREATE INDEX IF NOT EXISTS idx_or_daily_model
  ON openrouter_daily(model_id);


-- ─── openrouter_weekly ──────────────────────────────────────────
-- Per-week per-model breakdown from the app page's `weekly` series,
-- which spans the app's whole lifetime. This is what openrouter_lifetime
-- on /v1/stats sums. Until 2026-10-08 the scraper folded these rows
-- into openrouter_daily keyed by week-start date, so a week's total
-- overwrote that day's value and "lifetime" roughly doubled.
CREATE TABLE IF NOT EXISTS openrouter_weekly (
  week_start        TEXT NOT NULL,
  app               TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tokens            INTEGER NOT NULL,
  scraped_at        INTEGER NOT NULL,
  PRIMARY KEY (week_start, app, model_id)
);


-- ─── install_events ─────────────────────────────────────────────
-- One row per install, fired by install.sh / install.ps1. INSERT OR
-- IGNORE keeps the first-install timestamp authoritative.
CREATE TABLE IF NOT EXISTS install_events (
  installation_id   TEXT PRIMARY KEY,
  version           TEXT,
  os                TEXT,
  install_method    TEXT,
  installed_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_install_events_installed_at
  ON install_events(installed_at);
