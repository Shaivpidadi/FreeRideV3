-- D1 schema for the FreeRide telemetry beacon receiver.
-- Apply with:  wrangler d1 execute freeride-telemetry --file=./schema.sql

CREATE TABLE IF NOT EXISTS beacons (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  installation_id   TEXT NOT NULL,
  version           TEXT,
  os                TEXT,
  tokens_served     INTEGER NOT NULL DEFAULT 0,
  request_count     INTEGER NOT NULL DEFAULT 0,
  providers_active  TEXT,         -- JSON array
  uptime_hours      INTEGER NOT NULL DEFAULT 0,
  received_at       INTEGER NOT NULL  -- unix epoch seconds, server-set
);

CREATE INDEX IF NOT EXISTS idx_beacons_received_at
  ON beacons(received_at);

CREATE INDEX IF NOT EXISTS idx_beacons_installation_id
  ON beacons(installation_id);

-- Per-fetch snapshot of OpenRouter app-level token totals for both
-- the V2 (Shaivpidadi/FreeRide) and V3 (Shaivpidadi/FreeRideV3) apps.
-- OpenRouter doesn't expose a programmatic API for this, so the
-- Worker scrapes the SSR'd HTML on the public app page and parses
-- the inlined `"totalTokens":N` integer. Cron-driven (every 6 hours
-- per wrangler.toml triggers).
CREATE TABLE IF NOT EXISTS openrouter_aggregate (
  fetched_at        INTEGER PRIMARY KEY,  -- unix epoch seconds
  v1_tokens         INTEGER NOT NULL,     -- 30-day token count for FreeRide V2
  v3_tokens         INTEGER NOT NULL,     -- 30-day token count for FreeRideV3
  combined_tokens   INTEGER NOT NULL      -- v1_tokens + v3_tokens
);

-- Per-day per-model breakdown of OR app activity. The same scraper
-- that populates openrouter_aggregate now also extracts the
-- `{"x":"YYYY-MM-DD ...","ys":{"model":N, ...}}` series the OR app
-- page embeds, and upserts one row per (date, app, model) here.
-- Lets us answer "which models drove this week's traffic", "what's
-- our daily token series", etc. — both for BD pitch material and
-- for showing a richer chart on the marketing site.
--
-- (date, app, model_id) PK means upserts replace existing values
-- for that bucket on every cron, so the OR-side rolling 30-day
-- window naturally falls off as old days stop appearing in the page.
CREATE TABLE IF NOT EXISTS openrouter_daily (
  date              TEXT NOT NULL,        -- YYYY-MM-DD (UTC, OR's bucket)
  app               TEXT NOT NULL,        -- 'v1' or 'v3'
  model_id          TEXT NOT NULL,        -- e.g. 'qwen/qwen3-8b-04-28'
  tokens            INTEGER NOT NULL,
  scraped_at        INTEGER NOT NULL,     -- unix epoch seconds, last refresh
  PRIMARY KEY (date, app, model_id)
);

CREATE INDEX IF NOT EXISTS idx_or_daily_date
  ON openrouter_daily(date);

CREATE INDEX IF NOT EXISTS idx_or_daily_model
  ON openrouter_daily(model_id);

-- One row per install — fired by install.sh / install.ps1 right after
-- the freeride binary is on PATH, BEFORE the user runs anything. This
-- is the source of truth for adoption / install velocity, since the
-- existing `beacons` table only sees CLIs that have actually run
-- `freeride serve` for >1h with telemetry on (which most one-shot or
-- short-session users never reach).
--
-- PRIMARY KEY (installation_id) + INSERT OR IGNORE means re-running
-- the installer is idempotent — first-install timestamp wins and we
-- don't double-count. The install_id is the SAME UUIDv4 the gateway
-- later persists at ~/.freeride/installation_id, so beacon rows can
-- be correlated to their original install event by the same id.
CREATE TABLE IF NOT EXISTS install_events (
  installation_id   TEXT PRIMARY KEY,    -- UUIDv4
  version           TEXT,                -- freeride version at install time
  os                TEXT,                -- 'darwin' | 'linux' | 'windows' | 'other'
  install_method    TEXT,                -- 'curl-sh' | 'powershell'
  installed_at      INTEGER NOT NULL     -- unix epoch seconds, server-set
);

CREATE INDEX IF NOT EXISTS idx_install_events_installed_at
  ON install_events(installed_at);
