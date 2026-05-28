-- Postgres schema for the FreeRide telemetry receiver, hosted on Neon.
-- Apply with:
--   PGURL='postgresql://...' psql "$PGURL" -f schema.pg.sql
-- or via the worker's `wrangler secret put DATABASE_URL` path:
--   wrangler dev --remote, then visit /admin/init-schema (not implemented;
--   we apply once by hand and forget).
--
-- This file is the canonical source of truth for the Neon schema.
-- ``schema.sql`` (D1) is kept around as a frozen reference of the old
-- shape — every column is either preserved as-is or upgraded to a
-- Postgres-native type (BIGINT for unix-epoch ints, JSONB for the
-- providers_active blob, BIGSERIAL for autoincrement).

-- ─── beacons ────────────────────────────────────────────────────
-- One row per heartbeat from a running freeride install. Written by
-- POST /v1/beacon. NEVER stores IPs / hostnames / cf-connecting-ip.
CREATE TABLE IF NOT EXISTS beacons (
  id                BIGSERIAL PRIMARY KEY,
  installation_id   TEXT NOT NULL,
  version           TEXT,
  os                TEXT,
  tokens_served     BIGINT NOT NULL DEFAULT 0,
  request_count     BIGINT NOT NULL DEFAULT 0,
  providers_active  JSONB,
  uptime_hours      INTEGER NOT NULL DEFAULT 0,
  received_at       BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_beacons_received_at
  ON beacons(received_at);

CREATE INDEX IF NOT EXISTS idx_beacons_installation_id
  ON beacons(installation_id);


-- ─── openrouter_aggregate ───────────────────────────────────────
-- Per-fetch snapshot of OpenRouter app-level token totals for V1 +
-- V3 apps. Filled by the hourly cron scraper.
CREATE TABLE IF NOT EXISTS openrouter_aggregate (
  fetched_at        BIGINT PRIMARY KEY,
  v1_tokens         BIGINT NOT NULL,
  v3_tokens         BIGINT NOT NULL,
  combined_tokens   BIGINT NOT NULL
);


-- ─── openrouter_daily ───────────────────────────────────────────
-- Per-day per-model breakdown from the same scraper. Drives the
-- daily chart + top-models table on /models, and the lifetime
-- counter on the homepage.
CREATE TABLE IF NOT EXISTS openrouter_daily (
  date              TEXT NOT NULL,
  app               TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tokens            BIGINT NOT NULL,
  scraped_at        BIGINT NOT NULL,
  PRIMARY KEY (date, app, model_id)
);

CREATE INDEX IF NOT EXISTS idx_or_daily_date
  ON openrouter_daily(date);

CREATE INDEX IF NOT EXISTS idx_or_daily_model
  ON openrouter_daily(model_id);


-- ─── install_events ─────────────────────────────────────────────
-- One row per install — fired by install.sh / install.ps1.
-- INSERT ON CONFLICT DO NOTHING (the Postgres equivalent of D1's
-- INSERT OR IGNORE) keeps the first-install timestamp authoritative.
CREATE TABLE IF NOT EXISTS install_events (
  installation_id   TEXT PRIMARY KEY,
  version           TEXT,
  os                TEXT,
  install_method    TEXT,
  installed_at      BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_install_events_installed_at
  ON install_events(installed_at);
