-- Incremental rollup repair. Replays exactly the beacons whose rollup
-- step never ran (rolled = 0) through the same reset-aware, capped delta
-- logic the worker applies on write. Use it when the write path's
-- rollup batch failed for a while (D1 rejecting reads after the daily
-- budget was spent, for example) but the raw INSERTs went through.
--
-- A first version inferred the gap from each install's last_seen
-- watermark and missed rows whenever the install beaconed again after
-- the outage (153 such rows on 2026-10-08). The rolled flag removes the
-- guesswork. Semantics for a missed row:
--   * nothing newer from that install was rolled: full delta, as the
--     worker would have computed it;
--   * a newer beacon was already rolled ("covered"): its delta already
--     carried these tokens (counters are cumulative), so the missed row
--     adds 0 tokens and only counts as a beacon in its hour.
-- Totals and beacon counts therefore come out exact; the hour that the
-- covered tokens are attributed to stays with the later beacon. Run
-- rebuild_rollups.sql instead if exact per-hour attribution matters.
--
-- Cost: bounded by the partial index on rolled = 0. Idempotent: replayed
-- rows are flipped to rolled = 1 at the end.
--
--   wrangler d1 execute freeride-telemetry-d1 --remote --file=./repair_rollups.sql
--
-- Caps and cutoff must stay identical to DELTA_CAPS / DELTA_CAP_SINCE in
-- src/worker.js and to rebuild_rollups.sql.

INSERT INTO hourly_totals (hour, tokens_served, input_tokens, output_tokens, request_count, beacons)
WITH missed AS (
  SELECT b.id, b.installation_id, b.received_at,
         b.tokens_served, b.input_tokens, b.output_tokens, b.request_count,
         s.last_tokens_served AS s_ts, s.last_input_tokens AS s_it,
         s.last_output_tokens AS s_ot, s.last_request_count AS s_rc,
         s.last_seen AS s_last_seen
  FROM beacons b
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  WHERE b.rolled = 0
),
ordered AS (
  SELECT *,
         COALESCE(LAG(tokens_served) OVER w, s_ts) AS p_ts,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc,
         (s_last_seen IS NOT NULL AND received_at <= s_last_seen) AS covered
  FROM missed
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END, 250000000) ELSE CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END END END AS d_ts,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END, 250000000) ELSE CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END END END AS d_it,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000) ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END END AS d_ot,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000) ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END END AS d_rc
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

INSERT INTO totals (id, installations, tokens_served, input_tokens, output_tokens, request_count, beacons, since)
WITH missed AS (
  SELECT b.id, b.installation_id, b.received_at,
         b.tokens_served, b.input_tokens, b.output_tokens, b.request_count,
         s.last_tokens_served AS s_ts, s.last_input_tokens AS s_it,
         s.last_output_tokens AS s_ot, s.last_request_count AS s_rc,
         s.last_seen AS s_last_seen
  FROM beacons b
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  WHERE b.rolled = 0
),
ordered AS (
  SELECT *,
         COALESCE(LAG(tokens_served) OVER w, s_ts) AS p_ts,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc,
         (s_last_seen IS NOT NULL AND received_at <= s_last_seen) AS covered
  FROM missed
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END, 250000000) ELSE CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END END END AS d_ts,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END, 250000000) ELSE CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END END END AS d_it,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000) ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END END AS d_ot,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000) ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END END AS d_rc
  FROM ordered
)
SELECT 1,
       (SELECT COUNT(DISTINCT installation_id) FROM missed WHERE s_last_seen IS NULL),
       COALESCE(SUM(d_ts), 0), COALESCE(SUM(d_it), 0), COALESCE(SUM(d_ot), 0), COALESCE(SUM(d_rc), 0),
       COUNT(*), MIN(received_at)
FROM deltas
HAVING COUNT(*) > 0
ON CONFLICT(id) DO UPDATE SET
  installations = totals.installations + excluded.installations,
  tokens_served = totals.tokens_served + excluded.tokens_served,
  input_tokens  = totals.input_tokens  + excluded.input_tokens,
  output_tokens = totals.output_tokens + excluded.output_tokens,
  request_count = totals.request_count + excluded.request_count,
  beacons       = totals.beacons + excluded.beacons,
  since         = COALESCE(MIN(totals.since, excluded.since), totals.since, excluded.since);

-- ─── per-provider rollups for un-rolled rows ────────────────────
-- Runs BEFORE the install_state upsert below: `covered` must be judged
-- against the watermark as it was when the rows were missed, not the one
-- this repair is about to advance.
-- Same covered semantics: a row older than the install's watermark adds
-- 0 tokens (the newer rolled beacon carried them) and touches no state.
INSERT INTO install_provider_state
  (installation_id, provider, last_input_tokens, last_output_tokens, last_request_count,
   acc_input_tokens, acc_output_tokens, acc_request_count)
WITH rows AS (
  SELECT b.installation_id, b.received_at, b.id, je.key AS provider,
         CAST(COALESCE(json_extract(je.value, '$.input_tokens'), 0) AS INTEGER)  AS input_tokens,
         CAST(COALESCE(json_extract(je.value, '$.output_tokens'), 0) AS INTEGER) AS output_tokens,
         CAST(COALESCE(json_extract(je.value, '$.request_count'), 0) AS INTEGER) AS request_count,
         ps.last_input_tokens AS s_it, ps.last_output_tokens AS s_ot, ps.last_request_count AS s_rc,
         (s.last_seen IS NOT NULL AND b.received_at <= s.last_seen) AS covered
  FROM beacons b
  JOIN json_each(b.providers) je
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  LEFT JOIN install_provider_state ps ON ps.installation_id = b.installation_id AND ps.provider = je.key
  WHERE b.rolled = 0 AND b.providers IS NOT NULL
),
ordered AS (
  SELECT *,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc,
         ROW_NUMBER() OVER (PARTITION BY installation_id, provider ORDER BY received_at DESC, id DESC) AS rn_desc
  FROM rows
  WHERE NOT covered
  WINDOW w AS (PARTITION BY installation_id, provider ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, provider, rn_desc, input_tokens, output_tokens, request_count,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END, 250000000) ELSE CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END END AS d_it,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000) ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END AS d_ot,
         CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000) ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END AS d_rc
  FROM ordered
)
SELECT installation_id, provider,
       MAX(CASE WHEN rn_desc = 1 THEN input_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN output_tokens END),
       MAX(CASE WHEN rn_desc = 1 THEN request_count END),
       SUM(d_it), SUM(d_ot), SUM(d_rc)
FROM deltas
GROUP BY installation_id, provider
ON CONFLICT(installation_id, provider) DO UPDATE SET
  last_input_tokens  = excluded.last_input_tokens,
  last_output_tokens = excluded.last_output_tokens,
  last_request_count = excluded.last_request_count,
  acc_input_tokens   = install_provider_state.acc_input_tokens  + excluded.acc_input_tokens,
  acc_output_tokens  = install_provider_state.acc_output_tokens + excluded.acc_output_tokens,
  acc_request_count  = install_provider_state.acc_request_count + excluded.acc_request_count;

-- provider_totals is a pure aggregate of install_provider_state; recompute it.
DELETE FROM provider_totals;
INSERT INTO provider_totals (provider, installations, input_tokens, output_tokens, request_count)
SELECT provider, COUNT(*), SUM(acc_input_tokens), SUM(acc_output_tokens), SUM(acc_request_count)
FROM install_provider_state
GROUP BY provider;

INSERT INTO install_state
  (installation_id, version, os, first_seen, last_seen,
   last_tokens_served, last_input_tokens, last_output_tokens, last_request_count,
   acc_tokens_served, acc_input_tokens, acc_output_tokens, acc_request_count)
WITH missed AS (
  SELECT b.id, b.installation_id, b.received_at, b.version, b.os,
         b.tokens_served, b.input_tokens, b.output_tokens, b.request_count,
         s.last_tokens_served AS s_ts, s.last_input_tokens AS s_it,
         s.last_output_tokens AS s_ot, s.last_request_count AS s_rc,
         s.last_seen AS s_last_seen
  FROM beacons b
  LEFT JOIN install_state s ON s.installation_id = b.installation_id
  WHERE b.rolled = 0
),
ordered AS (
  SELECT *,
         COALESCE(LAG(tokens_served) OVER w, s_ts) AS p_ts,
         COALESCE(LAG(input_tokens)  OVER w, s_it) AS p_it,
         COALESCE(LAG(output_tokens) OVER w, s_ot) AS p_ot,
         COALESCE(LAG(request_count) OVER w, s_rc) AS p_rc,
         (s_last_seen IS NOT NULL AND received_at <= s_last_seen) AS covered,
         ROW_NUMBER() OVER (PARTITION BY installation_id ORDER BY received_at DESC, id DESC) AS rn_desc
  FROM missed
  WINDOW w AS (PARTITION BY installation_id ORDER BY received_at, id)
),
deltas AS (
  SELECT installation_id, received_at, version, os, rn_desc,
         tokens_served, input_tokens, output_tokens, request_count,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END, 250000000) ELSE CASE WHEN p_ts IS NULL OR tokens_served < p_ts THEN tokens_served ELSE tokens_served - p_ts END END END AS d_ts,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END, 250000000) ELSE CASE WHEN p_it IS NULL OR input_tokens < p_it THEN input_tokens ELSE input_tokens - p_it END END END AS d_it,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END, 50000000) ELSE CASE WHEN p_ot IS NULL OR output_tokens < p_ot THEN output_tokens ELSE output_tokens - p_ot END END END AS d_ot,
         CASE WHEN covered THEN 0 ELSE CASE WHEN received_at >= 1791385200 THEN MIN(CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END, 100000) ELSE CASE WHEN p_rc IS NULL OR request_count < p_rc THEN request_count ELSE request_count - p_rc END END END AS d_rc
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
  version            = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.version            ELSE install_state.version            END,
  os                 = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.os                 ELSE install_state.os                 END,
  last_tokens_served = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.last_tokens_served ELSE install_state.last_tokens_served END,
  last_input_tokens  = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.last_input_tokens  ELSE install_state.last_input_tokens  END,
  last_output_tokens = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.last_output_tokens ELSE install_state.last_output_tokens END,
  last_request_count = CASE WHEN excluded.last_seen > install_state.last_seen THEN excluded.last_request_count ELSE install_state.last_request_count END,
  first_seen         = MIN(install_state.first_seen, excluded.first_seen),
  last_seen          = MAX(install_state.last_seen, excluded.last_seen),
  acc_tokens_served  = install_state.acc_tokens_served + excluded.acc_tokens_served,
  acc_input_tokens   = install_state.acc_input_tokens  + excluded.acc_input_tokens,
  acc_output_tokens  = install_state.acc_output_tokens + excluded.acc_output_tokens,
  acc_request_count  = install_state.acc_request_count + excluded.acc_request_count;

UPDATE beacons SET rolled = 1 WHERE rolled = 0;
