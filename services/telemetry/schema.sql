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
