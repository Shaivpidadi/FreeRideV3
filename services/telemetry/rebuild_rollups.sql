-- Rebuild install_state and hourly_totals from the raw beacons log.
-- Deterministic: the same reset-aware delta-sum the worker applies
-- incrementally on each beacon write. Run once after a bulk import,
-- or any time the rollups are suspected to have drifted:
--
--   wrangler d1 execute freeride-telemetry --remote --file=./rebuild_rollups.sql
--
-- Cost: one scan of beacons per statement (row reads ~= 3 x row count).
-- Keep this for one-off maintenance, never on the request path.

DELETE FROM install_state;
DELETE FROM hourly_totals;

INSERT INTO install_state
  (installation_id, version, os, first_seen, last_seen,
   last_tokens_served, last_input_tokens, last_output_tokens, last_request_count,
   acc_tokens_served, acc_input_tokens, acc_output_tokens, acc_request_count)
WITH ordered AS (
  SELECT installation_id, received_at, id, version, os,
         tokens_served, input_tokens, output_tokens, request_count,
         LAG(tokens_served) OVER w AS p_ts,
         LAG(input_tokens)  OVER w AS p_it,
         LAG(output_tokens) OVER w AS p_ot,
         LAG(request_count) OVER w AS p_rc,
         ROW_NUMBER() OVER (PARTITION BY installation_id ORDER BY received_at DESC, id DESC) AS rn_desc
  FROM beacons
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at, version, os, rn_desc,
         tokens_served, input_tokens, output_tokens, request_count,
         CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END AS d_ts,
         CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END AS d_it,
         CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END AS d_ot,
         CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END AS d_rc
  FROM ordered
)
SELECT installation_id,
       MAX(CASE WHEN rn_desc = 1 THEN version END),
       MAX(CASE WHEN rn_desc = 1 THEN os END),
       MIN(received_at),
       MAX(received_at),
       MAX(CASE WHEN rn_desc = 1 THEN tokens_served END),
       MAX(CASE WHEN rn_desc = 1 THEN input_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN output_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN request_count END),
       SUM(d_ts), SUM(d_it), SUM(d_ot), SUM(d_rc)
FROM deltas
GROUP BY installation_id;

INSERT INTO hourly_totals (hour, tokens_served, input_tokens, output_tokens, request_count, beacons)
WITH ordered AS (
  SELECT installation_id, received_at,
         tokens_served, input_tokens, output_tokens, request_count,
         LAG(tokens_served) OVER w AS p_ts,
         LAG(input_tokens)  OVER w AS p_it,
         LAG(output_tokens) OVER w AS p_ot,
         LAG(request_count) OVER w AS p_rc
  FROM beacons
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
)
SELECT received_at - (received_at % 3600) AS hour,
       SUM(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END),
       SUM(CASE WHEN p_it IS NULL OR input_tokens  < p_it THEN input_tokens  ELSE input_tokens  - p_it END),
       SUM(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END),
       SUM(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END),
       COUNT(*)
FROM ordered
GROUP BY hour;
