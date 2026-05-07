# freeride-telemetry — backend service

Cloudflare Worker that receives FreeRide's anonymous aggregate beacon
and stores it in a D1 (SQLite-at-the-edge) database. Powers the public
"X tokens served by N installs" stat on the FreeRide homepage.

This service lives in the same repo as the gateway (`services/`) but is
deployed independently. It is **not** included in the Python package
shipped to PyPI — see `packages = ["freeride"]` in `pyproject.toml`.

## What it does

* **POST `/v1/beacon`** — accepts a FreeRide beacon JSON payload, stores
  one row in `beacons` table, returns `{ok: true}`.
* **GET `/v1/stats`** — returns aggregate counters across all beacons:
  installations, tokens_served, request_count, last 24h delta.
* **GET `/health`** — `{"ok": true}` for monitoring.

## What it stores

Each beacon row contains exactly the public payload shape (also asserted
by `tests/test_telemetry.py`):

| column            | type    | source                                          |
|-------------------|---------|-------------------------------------------------|
| installation_id   | TEXT    | client UUID4 (random per install, opaque)       |
| version           | TEXT    | freeride version string                         |
| os                | TEXT    | darwin / linux / windows / other                |
| tokens_served     | INTEGER | sum of input+output tokens since last beacon    |
| request_count     | INTEGER | request count since last beacon                 |
| providers_active  | TEXT    | JSON array, e.g. `["openrouter","nvidia_nim"]`  |
| uptime_hours      | INTEGER | gateway uptime hours                            |
| received_at       | INTEGER | server-side unix epoch (added by worker)        |

What's **not** stored: prompts, completions, model IDs, API keys, IPs
(the worker explicitly does not read `cf-connecting-ip` or log it).

## Deploy (one-time)

Prereq: a Cloudflare account (free) and `wrangler` CLI.

```bash
cd services/telemetry/

# 1) Create a D1 database
wrangler d1 create freeride-telemetry
# wrangler prints a database_id — paste it into wrangler.toml

# 2) Apply schema
wrangler d1 execute freeride-telemetry --file=./schema.sql

# 3) Deploy the worker
wrangler deploy

# 4) Bind the custom domain (optional but recommended)
#    Add CNAME `telemetry` -> <worker>.workers.dev in Cloudflare DNS for
#    free-ride.xyz, OR set up a Routes binding in wrangler.toml.
```

See https://developers.cloudflare.com/workers/get-started/guide/ if any of
these steps are unfamiliar.

## Cost

Free tier covers everything we'll see in v0.3.x:
* 100,000 requests/day (we'd need ~4,000 active installs to dent this)
* 5 GB D1 storage (~10M beacon rows)
* No hostnames, IPs, or content stored — purely numeric counters

Total expected ongoing cost: **$0.00**.

## Observability

```bash
# Latest 10 beacons
wrangler d1 execute freeride-telemetry --command \
  "SELECT * FROM beacons ORDER BY received_at DESC LIMIT 10"

# Aggregate
wrangler d1 execute freeride-telemetry --command \
  "SELECT COUNT(DISTINCT installation_id) AS installs,
          SUM(tokens_served) AS tokens
   FROM beacons"
```
