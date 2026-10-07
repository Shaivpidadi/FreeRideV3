-- Incremental rollup repair. Replays only the beacons newer than each
-- install's install_state.last_seen (and beacons of installs with no
-- state row) through the same reset-aware, capped delta logic the worker
-- applies on write. Use it when the write path's rollup step failed for
-- a while (for example D1 rejecting reads after the daily budget was
-- spent) but the raw INSERTs went through.
--
-- Cost: the beacons scan is bounded by the received_at index, so it
-- reads only rows since __SINCE__ (unix epoch; set it a little before
-- the first suspected gap) plus one install_state lookup per row. Safe
-- to run repeatedly: an already-rolled-up beacon is never newer than its
-- install's last_seen, so it is skipped.
--
--   sed 's/__SINCE__/1791385200/' repair_rollups.sql > /tmp/repair.sql
--   wrangler d1 execute freeride-telemetry-d1 --remote --file=/tmp/repair.sql
--
-- Caps and cutoff must stay identical to DELTA_CAPS / DELTA_CAP_SINCE in
-- src/worker.js and to rebuild_rollups.sql.

INSERT INTO hourly_totals (hour, tokens_served, input_tokens, output_tokens, request_count, beacons)
WITH missed AS (
  SELECT b.id, b.installation_id, b.received_at,
         b.tokens_served, b.input_tokens, b.output_tokens, b.request_count,
         s.last_tokens_served AS s_ts, s.last_input_tokens AS s_it,
         s.last_output_tokens AS s_ot, s.last_request_count AS s_rc
  FROM beacons b
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  WHERE b.received_at >= __SINCE__
    AND (s.installation_id IS NULL OR b.received_at > s.last_seen)
),
ordered AS (
  SELECT *,
         COALESCE(LAG(tokens_served) OVER w, s_ts) AS p_ts,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc
  FROM missed
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END, 250000000)
              ELSE CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END END AS d_ts,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END, 250000000)
              ELSE CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END END AS d_it,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000)
              ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END AS d_ot,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000)
              ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END AS d_rc
  FROM ordered
)
SELECT received_at - (received_at % 3600), SUM(d_ts), SUM(d_it), SUM(d_ot), SUM(d_rc), COUNT(*)
FROM deltas
GROUP BY received_at - (received_at % 3600)
ON CONFLICT(hour) DO UPDATE SET
  tokens_served = hourly_totals.tokens_served + excluded.tokens_served,
  input_tokens  = hourly_totals.input_tokens  + excluded.input_tokens,
  output_tokens = hourly_totals.output_tokens + excluded.output_tokens,
  request_count = hourly_totals.request_count + excluded.request_count,
  beacons       = hourly_totals.beacons       + excluded.beacons;

INSERT INTO install_state
  (installation_id, version, os, first_seen, last_seen,
   last_tokens_served, last_input_tokens, last_output_tokens, last_request_count,
   acc_tokens_served, acc_input_tokens, acc_output_tokens, acc_request_count)
WITH missed AS (
  SELECT b.id, b.installation_id, b.received_at, b.version, b.os,
         b.tokens_served, b.input_tokens, b.output_tokens, b.request_count,
         s.last_tokens_served AS s_ts, s.last_input_tokens AS s_it,
         s.last_output_tokens AS s_ot, s.last_request_count AS s_rc
  FROM beacons b
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  WHERE b.received_at >= __SINCE__
    AND (s.installation_id IS NULL OR b.received_at > s.last_seen)
),
ordered AS (
  SELECT *,
         COALESCE(LAG(tokens_served) OVER w, s_ts) AS p_ts,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc,
         ROW_NUMBER() OVER (PARTITION BY installation_id ORDER BY received_at DESC, id DESC) AS rn_desc
  FROM missed
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at, version, os, rn_desc,
         tokens_served, input_tokens, output_tokens, request_count,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END, 250000000)
              ELSE CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END END AS d_ts,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END, 250000000)
              ELSE CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END END AS d_it,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000)
              ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END AS d_ot,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000)
              ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END AS d_rc
  FROM ordered
)
SELECT installation_id,
       MAX(CASE WHEN rn_desc = 1 THEN version END),
       MAX(CASE WHEN rn_desc = 1 THEN os END),
       MIN(received_at), MAX(received_at),
       MAX(CASE WHEN rn_desc = 1 THEN tokens_served END),
       MAX(CASE WHEN rn_desc = 1 THEN input_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN output_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN request_count END),
       SUM(d_ts), SUM(d_it), SUM(d_ot), SUM(d_rc)
FROM deltas
GROUP BY installation_id
ON CONFLICT(installation_id) DO UPDATE SET
  version = excluded.version,
  os = excluded.os,
  last_seen = excluded.last_seen,
  last_tokens_served = excluded.last_tokens_served,
  last_input_tokens  = excluded.last_input_tokens,
  last_output_tokens = excluded.last_output_tokens,
  last_request_count = excluded.last_request_count,
  acc_tokens_served = install_state.acc_tokens_served + excluded.acc_tokens_served,
  acc_input_tokens  = install_state.acc_input_tokens  + excluded.acc_input_tokens,
  acc_output_tokens = install_state.acc_output_tokens + excluded.acc_output_tokens,
  acc_request_count = install_state.acc_request_count + excluded.acc_request_count;
