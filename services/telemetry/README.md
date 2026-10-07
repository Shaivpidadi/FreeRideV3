# freeride-telemetry — backend service

Cloudflare Worker that receives FreeRide's anonymous aggregate beacon,
stores it in **D1** (SQLite at the edge), and serves the public
"X tokens served by N installs" numbers from a **KV** snapshot. Powers
the live counter on the FreeRide homepage and the `/models` charts.

This service lives in the same repo as the gateway (`services/`) but is
deployed independently. It is **not** included in the Python package
shipped to PyPI.

## What it does

* **POST `/v1/beacon`** — accepts a beacon, appends one row to
  `beacons`, and folds it into two rollups: `install_state` (one row
  per install: last reported counters + reset-aware lifetime delta-sum)
  and `hourly_totals` (delta-sum per wall-clock hour).
* **POST `/v1/install-event`** — records a first install (idempotent).
* **GET `/v1/stats`** — the aggregate payload. Served from the Cache
  API (5 min) in front of a KV snapshot that the hourly cron
  recomputes from the rollup tables. The beacons table is never scanned
  on the request path.
* **GET `/health`** — `{"ok": true}` for monitoring.
* **GET `/install.sh`, `/install.ps1`, `/ridex.sh`** — installer scripts
  (embedded copies of the repo-root files; keep in sync by hand).
* **POST `/v1/_admin/refresh-openrouter`**, **`/v1/_admin/recompute-stats`**
  — require `Authorization: Bearer <ADMIN_TOKEN>`; disabled when the
  secret is unset.

## Why D1 + KV (and not Neon)

Storage went D1 → Neon (2026-05-28) → D1 (2026-10-07). Neon bills
compute-hours and needs 5 idle minutes to suspend; hourly beacons from
every install, the site's 5-minute stats poll, and the hourly cron
meant it never did, so a few hundred rows a day cost about $20/month.
D1 bills rows scanned, which only works if reads stay small — hence the
write-time rollups and the KV snapshot. Expected cost at current volume
(about 800 beacons/day, a few thousand D1 row reads per hour): $0.

The Neon projects were **not** deleted; they hold a read-only copy of
everything up to the cutover. `schema.pg.sql`, `migrate_d1_to_neon.py`
and `sync_dbs.py` are kept as historical reference.

## What it stores

Each beacon row contains exactly the public payload shape (asserted by
`tests/test_telemetry.py`):

| column | type | source |
|-------------------|---------|-------------------------------------------------|
| installation_id | TEXT | client UUID4 (random per install, opaque) |
| version | TEXT | freeride version string |
| os | TEXT | darwin / linux / windows / other |
| tokens_served | INTEGER | cumulative input+output tokens for the install |
| input_tokens | INTEGER | cumulative prompt tokens |
| output_tokens | INTEGER | cumulative completion tokens |
| request_count | INTEGER | cumulative request count |
| providers_active | TEXT | JSON array, e.g. `["openrouter","nvidia_nim"]` |
| uptime_hours | INTEGER | gateway uptime hours |
| received_at | INTEGER | server-side unix epoch (added by worker) |

What's **not** stored: prompts, completions, model IDs, API keys, IPs
(the worker explicitly does not read `cf-connecting-ip` or log it).

## Deploy (one-time)

Prereqs: a Cloudflare account (free), Node 22+, `npm install`.

```bash
cd services/telemetry/
cp wrangler.example.toml wrangler.toml      # gitignored; holds your ids

npx wrangler login
npx wrangler d1 create freeride-telemetry    # paste database_id into wrangler.toml
npx wrangler kv namespace create STATS       # paste id into wrangler.toml
npm run schema:remote
npx wrangler secret put ADMIN_TOKEN          # any long random string
npx wrangler deploy
```

## Migrating data from Neon (done 2026-10-07; kept for reference)

```bash
# .dev.vars (gitignored): DATABASE_URL=... and optionally DATABASE_URL_B=...
python3 migrate_neon_to_d1.py --out ./d1-import
for f in ./d1-import/0*.sql; do npx wrangler d1 execute freeride-telemetry --remote --file=$f; done
npm run rollups:rebuild
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://api.free-ride.xyz/v1/_admin/recompute-stats
```

Every import statement is `INSERT OR IGNORE` on the natural key, so a
second pass with `--since <epoch>` after the cutover picks up the
beacons that landed on Neon in between, followed by one more rollup
rebuild.

## Abuse limits

The beacon and install-event endpoints are anonymous by design, so the
zone carries one WAF rate-limiting rule (the Free plan's allowance):
`POST /v1/beacon` and `/v1/install-event` on `api.` and `telemetry.`
are limited to 5 requests per 10 seconds per IP, blocked for 10
seconds. A real install sends one beacon an hour. The rule lives in the
Cloudflare dashboard under Security → WAF → Rate limiting rules; it is
not managed by wrangler.

The rate limit bounds how many beacons one IP can send; a per-beacon
increment ceiling bounds what each one can add. From the 2026-10-07
cutover on, an increment above 250M tokens (input or combined), 50M
output tokens or 100k requests is counted as the cap (`DELTA_CAPS` in
`src/worker.js`, mirrored in `rebuild_rollups.sql`). Every legitimate
hourly jump on record is under 75M tokens. The delta is capped rather
than dropped, so an install that overshoots after a telemetry outage
resumes normal counting on its next beacon. Earlier rows are never
capped, so history stays exact.

## Observability

```bash
npx wrangler tail
npx wrangler d1 execute freeride-telemetry --remote --command \
  "SELECT COUNT(*) AS installs, SUM(acc_tokens_served) AS tokens FROM install_state"
npx wrangler kv key get --binding STATS --remote stats:v1
```

`rebuild_rollups.sql` rebuilds `install_state` and `hourly_totals` from
the raw log if they ever drift; it scans `beacons` three times, so it is
a maintenance tool, never a request-path query.
