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
