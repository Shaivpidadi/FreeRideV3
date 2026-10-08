// FreeRide site + telemetry beacon receiver — Cloudflare Worker + D1 + KV.
//
// Routes (no auth; counters and installer are public-by-design):
//   GET  /             — 301 → marketing site at https://free-ride.xyz/
//   GET  /install.sh   — the POSIX (macOS / Linux) installer
//   GET  /install.ps1  — the PowerShell (Windows) installer
//   GET  /ridex.sh     — the ridex agent installer
//   POST /v1/beacon    — accept a beacon, write a row to `beacons` and
//                        fold it into the install_state / hourly_totals
//                        rollups.
//   POST /v1/install-event — record a first install.
//   GET  /v1/stats     — aggregate counters, served from a KV snapshot
//                        that the hourly cron recomputes.
//   GET  /health       — `{ok: true}` for monitoring.
//   POST /v1/_admin/refresh-openrouter — re-scrape OR + recompute stats
//   POST /v1/_admin/recompute-stats    — recompute the KV snapshot
//        (both require `Authorization: Bearer <ADMIN_TOKEN>`).
//
// The worker explicitly does NOT log or store IPs / hostnames /
// `cf-connecting-ip`. Inputs we accept are exactly the public spec
// (the design plan); anything else is dropped.
//
// Storage: Cloudflare D1 (binding `DB`, schema in ./schema.d1.sql) for
// the raw beacon log and its rollups, Workers KV (binding `STATS`) for
// the /v1/stats snapshot. History: D1 → Neon Postgres (2026-05-28) →
// D1 again (2026-10-07). Neon bills compute-hours and the hourly
// beacons plus the 5-minute stats poll never let its compute suspend,
// so a few hundred rows a day cost ~$20/month. D1 bills rows scanned,
// which is why /v1/stats reads a KV snapshot and the rollups are
// maintained on write: the beacons table is never scanned on the
// request path. The Neon projects are left intact as a read-only
// backup; ./schema.pg.sql and ./migrate_d1_to_neon.py are kept as
// historical reference.
//
// The installer scripts are embedded as INSTALL_SH, INSTALL_PS1 and
// RIDEX_SH below — KEEP IN SYNC with /install.sh and /install.ps1 at
// the repo root (and install.sh in the ridex repo) by hand. The repo
// files are the source of truth; these are their public-facing copies.

const ALLOWED_OS = new Set(["darwin", "linux", "windows", "other"]);

const STATS_KV_KEY = "stats:v1";

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json",
      // Worker is fully anonymous — no auth, no cookies — so wildcard
      // CORS is safe and necessary so the marketing site (hosted on
      // a different origin) can client-fetch /v1/stats for the live
      // counter.
      "access-control-allow-origin": "*",
      // Everything served to clients goes out no-store: the zone edge
      // cache would otherwise stack on top of our own Cache API copy.
      "cache-control": "no-store",
    },
  });

// Per-beacon, per-field sanity bound — rejects garbage / malicious
// payloads, NOT a real usage ceiling. Beacons ship CUMULATIVE lifetime
// counters, so this has to sit far above any real install: 1e13 is
// ~6000× the heaviest install seen, while even every install maxed
// out stays under JS's 2^53 so SUM()s serialize as exact numbers.
function clampInt(value, max = 10_000_000_000_000) {
  const n = Number.isFinite(value) ? Math.floor(value) : 0;
  if (n < 0) return 0;
  if (n > max) return max;
  return n;
}

function sanitizeProviders(arr) {
  if (!Array.isArray(arr)) return [];
  return arr
    .filter((s) => typeof s === "string" && s.length <= 64)
    .slice(0, 10);
}

function sanitizeUuid(s) {
  if (typeof s !== "string") return null;
  // UUIDv4 shape; reject anything else to keep the column clean.
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(s)
  ) {
    return null;
  }
  return s.toLowerCase();
}

function sanitizeVersion(s) {
  if (typeof s !== "string") return "";
  if (s.length > 32) return "";
  if (!/^[0-9a-zA-Z.+\-]+$/.test(s)) return "";
  return s;
}

// Allowed install methods. Anything else collapses to 'other' so we
// keep the column tidy without rejecting otherwise-valid installs.
const ALLOWED_INSTALL_METHODS = new Set(["curl-sh", "powershell", "other"]);

async function handleInstallEvent(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "invalid_json" }, 400);
  }
  if (!body || typeof body !== "object") {
    return json({ ok: false, error: "bad_payload" }, 400);
  }

  const installation_id = sanitizeUuid(body.installation_id);
  if (!installation_id) {
    return json({ ok: false, error: "invalid_installation_id" }, 400);
  }

  const os = ALLOWED_OS.has(body.os) ? body.os : "other";
  const version = sanitizeVersion(body.version);
  const install_method = ALLOWED_INSTALL_METHODS.has(body.install_method)
    ? body.install_method
    : "other";

  // INSERT OR IGNORE: re-running the installer is idempotent. First
  // install timestamp wins.
  await env.DB.prepare(
    `INSERT OR IGNORE INTO install_events
       (installation_id, version, os, install_method, installed_at)
     VALUES (?1, ?2, ?3, ?4, ?5)`,
  )
    .bind(installation_id, version, os, install_method, Math.floor(Date.now() / 1000))
    .run();

  return json({ ok: true });
}

// Reset-aware increment: a counter that drops below the previous
// value is a reset (reinstall, cleared stats.json), so the full new
// value counts as freshly served. First beacon seeds with its value.
// Must stay identical to the CASE expressions in rebuild_rollups.sql.
function delta(current, previous) {
  if (previous == null || current < previous) return current;
  return current - previous;
}

// Per-beacon increment ceiling. Beacons are anonymous, so one forged
// report could otherwise add anything up to the 1e13 field cap to the
// public total. Every legitimate hourly jump on record is under 75M
// tokens; the only larger ones (679M, 312M, 240M) date from the old
// 1e9 clamp bug. Caps apply to beacons received from the 2026-10-07
// cutover on, so history is untouched, and the delta is CAPPED, not
// dropped, so an install that legitimately overshoots (long telemetry
// outage) resumes normal counting on its next beacon instead of
// getting stuck. Must stay identical to rebuild_rollups.sql.
const DELTA_CAP_SINCE = 1791385200; // 2026-10-07T15:00:00Z
const DELTA_CAPS = {
  tokens_served: 250_000_000,
  input_tokens: 250_000_000,
  output_tokens: 50_000_000,
  request_count: 100_000,
};
function cappedDelta(field, current, previous, received_at) {
  const d = delta(current, previous);
  if (received_at < DELTA_CAP_SINCE) return d;
  return d > DELTA_CAPS[field] ? DELTA_CAPS[field] : d;
}

async function handleBeacon(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "invalid_json" }, 400);
  }
  if (!body || typeof body !== "object") {
    return json({ ok: false, error: "bad_payload" }, 400);
  }

  const installation_id = sanitizeUuid(body.installation_id);
  if (!installation_id) {
    return json({ ok: false, error: "invalid_installation_id" }, 400);
  }

  const os = ALLOWED_OS.has(body.os) ? body.os : "other";
  const version = sanitizeVersion(body.version);
  const input_tokens = clampInt(body.input_tokens);
  const output_tokens = clampInt(body.output_tokens);
  // Old gateways only ship ``tokens_served``; new gateways ship both
  // the split fields AND ``tokens_served = input + output``. We
  // synthesize whichever the client didn't send so every row stays
  // self-consistent regardless of payload generation.
  const tokens_served = clampInt(
    body.tokens_served ?? input_tokens + output_tokens,
  );
  const request_count = clampInt(body.request_count);
  const uptime_hours = clampInt(body.uptime_hours, 24 * 365 * 10); // <= 10y
  const providers_active = sanitizeProviders(body.providers_active);
  const received_at = Math.floor(Date.now() / 1000);

  // 1. Raw log row. (installation_id, received_at) is unique; a
  //    same-second duplicate is ignored and must NOT touch the
  //    rollups, hence the changes check.
  const inserted = await env.DB.prepare(
    `INSERT OR IGNORE INTO beacons
       (installation_id, version, os,
        tokens_served, input_tokens, output_tokens,
        request_count, providers_active, uptime_hours, received_at, rolled)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0)`,
  )
    .bind(
      installation_id, version, os,
      tokens_served, input_tokens, output_tokens,
      request_count, JSON.stringify(providers_active), uptime_hours, received_at,
    )
    .run();
  if (!inserted.meta || inserted.meta.changes === 0) {
    return json({ ok: true, duplicate: true });
  }
  const beacon_row_id = inserted.meta.last_row_id;

  // 2. Fold into the rollups. One point read for the install's last
  //    counters, then one batch (install_state upsert + hourly bucket).
  const prev = await env.DB.prepare(
    `SELECT last_tokens_served, last_input_tokens, last_output_tokens, last_request_count
     FROM install_state WHERE installation_id = ?1`,
  )
    .bind(installation_id)
    .first();
  const d_ts = cappedDelta("tokens_served", tokens_served, prev?.last_tokens_served, received_at);
  const d_it = cappedDelta("input_tokens", input_tokens, prev?.last_input_tokens, received_at);
  const d_ot = cappedDelta("output_tokens", output_tokens, prev?.last_output_tokens, received_at);
  const d_rc = cappedDelta("request_count", request_count, prev?.last_request_count, received_at);
  if (
    d_ts !== delta(tokens_served, prev?.last_tokens_served) ||
    d_rc !== delta(request_count, prev?.last_request_count)
  ) {
    // Counted at the cap; the raw row keeps the reported value for
    // forensics. No IP is logged.
    console.warn(`beacon delta capped install=${installation_id.slice(0, 8)} tokens=${tokens_served}`);
  }
  const hour = received_at - (received_at % 3600);

  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO install_state
         (installation_id, version, os, first_seen, last_seen,
          last_tokens_served, last_input_tokens, last_output_tokens, last_request_count,
          acc_tokens_served, acc_input_tokens, acc_output_tokens, acc_request_count)
       VALUES (?1, ?2, ?3, ?4, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
       ON CONFLICT(installation_id) DO UPDATE SET
         version = excluded.version,
         os = excluded.os,
         last_seen = excluded.last_seen,
         last_tokens_served = excluded.last_tokens_served,
         last_input_tokens = excluded.last_input_tokens,
         last_output_tokens = excluded.last_output_tokens,
         last_request_count = excluded.last_request_count,
         acc_tokens_served = install_state.acc_tokens_served + excluded.acc_tokens_served,
         acc_input_tokens = install_state.acc_input_tokens + excluded.acc_input_tokens,
         acc_output_tokens = install_state.acc_output_tokens + excluded.acc_output_tokens,
         acc_request_count = install_state.acc_request_count + excluded.acc_request_count`,
    ).bind(
      installation_id, version, os, received_at,
      tokens_served, input_tokens, output_tokens, request_count,
      d_ts, d_it, d_ot, d_rc,
    ),
    env.DB.prepare(
      `INSERT INTO hourly_totals (hour, tokens_served, input_tokens, output_tokens, request_count, beacons)
       VALUES (?1, ?2, ?3, ?4, ?5, 1)
       ON CONFLICT(hour) DO UPDATE SET
         tokens_served = hourly_totals.tokens_served + excluded.tokens_served,
         input_tokens = hourly_totals.input_tokens + excluded.input_tokens,
         output_tokens = hourly_totals.output_tokens + excluded.output_tokens,
         request_count = hourly_totals.request_count + excluded.request_count,
         beacons = hourly_totals.beacons + 1`,
    ).bind(hour, d_ts, d_it, d_ot, d_rc),
    // Same batch as the rollup: if the batch fails, the row stays
    // rolled = 0 and repair_rollups.sql picks it up.
    env.DB.prepare(`UPDATE beacons SET rolled = 1 WHERE id = ?1`).bind(beacon_row_id),
  ]);

  return json({ ok: true });
}

// ---------------------------------------------------------------------------
// Stats snapshot
// ---------------------------------------------------------------------------
//
// Computed from the rollup tables only (install_state ~ hundreds of
// rows, hourly_totals 24-168 rows, openrouter_* ~1k rows): a few
// thousand D1 row reads per run. Runs hourly from the cron and on the
// admin endpoint; /v1/stats serves the KV copy. The JSON shape is
// unchanged from the Neon implementation.

const isoDate = (sec) => new Date(sec * 1000).toISOString().slice(0, 10);
const toNum = (v) => (v == null ? 0 : Number(v));

async function computeStats(env) {
  const nowSec = Math.floor(Date.now() / 1000);
  const day24Ago = nowSec - 24 * 3600;
  const day7Ago = nowSec - 7 * 24 * 3600;
  const day30Ago = nowSec - 30 * 24 * 3600;
  // Windows over hourly buckets start at the bucket containing the
  // cutoff, so "last 24h" covers 24 to 25 wall-clock hours. Beacons
  // are hourly, so this is the same resolution the data has.
  const bucket = (sec) => sec - (sec % 3600);

  const [all, active24, day, week, orRows, last7d, topModels, lifetime, installs] =
    await Promise.all([
      env.DB.prepare(
        `SELECT COUNT(*) AS installations, MIN(first_seen) AS since_ts,
                SUM(acc_tokens_served) AS tokens_served,
                SUM(acc_input_tokens)  AS input_tokens,
                SUM(acc_output_tokens) AS output_tokens,
                SUM(acc_request_count) AS request_count
         FROM install_state`,
      ).first(),
      env.DB.prepare(
        `SELECT COUNT(*) AS installations_24h FROM install_state WHERE last_seen > ?1`,
      ).bind(day24Ago).first(),
      env.DB.prepare(
        `SELECT SUM(tokens_served) AS tokens_served_24h, SUM(input_tokens) AS input_tokens_24h,
                SUM(output_tokens) AS output_tokens_24h, SUM(request_count) AS request_count_24h
         FROM hourly_totals WHERE hour >= ?1`,
      ).bind(bucket(day24Ago)).first(),
      env.DB.prepare(
        `SELECT SUM(tokens_served) AS tokens_7d FROM hourly_totals WHERE hour >= ?1`,
      ).bind(bucket(day7Ago)).first(),
      env.DB.prepare(
        `SELECT v1_tokens, v3_tokens, combined_tokens, fetched_at
         FROM openrouter_aggregate ORDER BY fetched_at DESC LIMIT 1`,
      ).first(),
      env.DB.prepare(
        `SELECT date, SUM(tokens) AS tokens, COUNT(DISTINCT model_id) AS models_count
         FROM openrouter_daily WHERE date >= ?1 GROUP BY date ORDER BY date DESC`,
      ).bind(isoDate(day7Ago)).all(),
      env.DB.prepare(
        `SELECT model_id, SUM(tokens) AS tokens
         FROM openrouter_daily WHERE date >= ?1
         GROUP BY model_id ORDER BY tokens DESC LIMIT 10`,
      ).bind(isoDate(day30Ago)).all(),
      env.DB.prepare(
        `SELECT SUM(tokens) AS combined_tokens,
                SUM(CASE WHEN app = 'v1' THEN tokens ELSE 0 END) AS v1_tokens,
                SUM(CASE WHEN app = 'v3' THEN tokens ELSE 0 END) AS v3_tokens,
                MIN(date) AS since, MAX(date) AS through
         FROM openrouter_daily`,
      ).first(),
      env.DB.prepare(
        `SELECT COUNT(*) AS total,
                SUM(CASE WHEN installed_at > ?1 THEN 1 ELSE 0 END) AS last_24h,
                SUM(CASE WHEN installed_at > ?2 THEN 1 ELSE 0 END) AS last_7d,
                SUM(CASE WHEN installed_at > ?3 THEN 1 ELSE 0 END) AS last_30d
         FROM install_events`,
      ).bind(day24Ago, day7Ago, day30Ago).first(),
    ]);

  // Live-tick rate (tokens/sec): trailing-7d average, falling back to
  // the lifetime average when 7d has no activity.
  const tokens7d = toNum(week?.tokens_7d);
  const sinceTs = toNum(all?.since_ts);
  const lifetimeSpan = sinceTs ? Math.max(1, nowSec - sinceTs) : 0;
  const ratePerSec =
    tokens7d > 0
      ? tokens7d / (7 * 24 * 3600)
      : lifetimeSpan
        ? toNum(all?.tokens_served) / lifetimeSpan
        : 0;

  return {
    object: "stats",
    as_of: new Date().toISOString(),
    total: {
      installations: toNum(all?.installations),
      tokens_served: toNum(all?.tokens_served),
      input_tokens: toNum(all?.input_tokens),
      output_tokens: toNum(all?.output_tokens),
      request_count: toNum(all?.request_count),
      since: sinceTs ? isoDate(sinceTs) : null,
      rate_per_sec: ratePerSec,
    },
    last_24h: {
      installations: toNum(active24?.installations_24h),
      tokens_served: toNum(day?.tokens_served_24h),
      input_tokens: toNum(day?.input_tokens_24h),
      output_tokens: toNum(day?.output_tokens_24h),
      request_count: toNum(day?.request_count_24h),
    },
    installs: {
      total: toNum(installs?.total),
      last_24h: toNum(installs?.last_24h),
      last_7d: toNum(installs?.last_7d),
      last_30d: toNum(installs?.last_30d),
    },
    openrouter_30d: orRows
      ? {
          v1_tokens: toNum(orRows.v1_tokens),
          v3_tokens: toNum(orRows.v3_tokens),
          combined_tokens: toNum(orRows.combined_tokens),
          fetched_at: toNum(orRows.fetched_at),
        }
      : null,
    openrouter_lifetime:
      lifetime && lifetime.combined_tokens
        ? {
            v1_tokens: toNum(lifetime.v1_tokens),
            v3_tokens: toNum(lifetime.v3_tokens),
            combined_tokens: toNum(lifetime.combined_tokens),
            since: lifetime.since,
            through: lifetime.through,
          }
        : null,
    openrouter_daily: {
      last_7d: (last7d?.results ?? []).map((r) => ({
        date: r.date,
        tokens: toNum(r.tokens),
        models_count: toNum(r.models_count),
      })),
      top_models_30d: (topModels?.results ?? []).map((r) => ({
        model_id: r.model_id,
        tokens: toNum(r.tokens),
      })),
    },
  };
}

async function recomputeStats(env) {
  const payload = await computeStats(env);
  await env.STATS.put(STATS_KV_KEY, JSON.stringify(payload));
  return payload;
}

// ---------------------------------------------------------------------------
// OpenRouter app-stats refresh (cron-driven)
// ---------------------------------------------------------------------------
//
// OpenRouter exposes per-app token totals via their public app activity
// pages but has no programmatic API for it. The relevant page server-side
// renders the data inline as JSON, so we fetch the HTML and pull the
// `\\"totalTokens\\":N` integer with a regex. Two pages, one for each
// referer (V2 and V3); we sum them so users see the combined community
// total rather than a number split by version.

const OR_APP_URLS = [
  {
    slug: "v1",
    url:
      "https://openrouter.ai/apps?url=" +
      encodeURIComponent("https://github.com/Shaivpidadi/FreeRide"),
  },
  {
    slug: "v3",
    url:
      "https://openrouter.ai/apps?url=" +
      encodeURIComponent("https://github.com/Shaivpidadi/FreeRideV3"),
  },
];

async function fetchOpenRouterAppHtml(url) {
  const resp = await fetch(url, {
    cf: { cacheTtl: 0 },
    headers: { "user-agent": "FreeRideTelemetryWorker/1.0" },
  });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} for ${url}`);
  }
  return resp.text();
}

function extractTotalTokens(html) {
  // The SSR'd payload has `\"totalTokens\":NNNNNNNN` embedded as
  // escaped JSON inside an RSC streaming chunk. Loose pattern so it
  // survives benign formatting changes on OR's side.
  const m = html.match(/\\"totalTokens\\":(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}

// Pull the per-day per-model breakdown the OR app page embeds. Each
// day appears as `\"x\":\"YYYY-MM-DD ...\",\"ys\":{model:N, model:N}`.
// Returns a flat list of {date, model_id, tokens}; the caller keys
// it by app slug. Only positive counts are kept.
function parseOpenRouterDailyBreakdown(html) {
  const out = [];
  const dayRe =
    /\\"x\\":\\"(\d{4}-\d{2}-\d{2})[^\\]*\\",\\"ys\\":\{([^}]+)\}/g;
  for (const dayMatch of html.matchAll(dayRe)) {
    const date = dayMatch[1];
    const ysRaw = dayMatch[2];
    const pairRe = /\\"([^\\"]+)\\":(\d+)/g;
    for (const pairMatch of ysRaw.matchAll(pairRe)) {
      const tokens = parseInt(pairMatch[2], 10);
      if (tokens > 0) {
        out.push({ date, model_id: pairMatch[1], tokens });
      }
    }
  }
  return out;
}

async function refreshOpenRouterAggregate(env) {
  const results = {};
  const breakdownByApp = {};
  for (const { slug, url } of OR_APP_URLS) {
    try {
      const html = await fetchOpenRouterAppHtml(url);
      results[slug] = extractTotalTokens(html);
      breakdownByApp[slug] = parseOpenRouterDailyBreakdown(html);
    } catch (e) {
      console.error("openrouter scrape failed for", slug, e);
      // Use the last known value as a fallback so a transient OR
      // outage doesn't reset the displayed number to zero. Column
      // name is whitelisted against the two slugs above.
      const col = slug === "v1" ? "v1_tokens" : "v3_tokens";
      const prev = await env.DB.prepare(
        `SELECT ${col} AS t FROM openrouter_aggregate ORDER BY fetched_at DESC LIMIT 1`,
      ).first();
      results[slug] = Number(prev?.t ?? 0);
      breakdownByApp[slug] = [];
    }
  }
  const v1 = results.v1 ?? 0;
  const v3 = results.v3 ?? 0;
  const combined = v1 + v3;
  const now = Math.floor(Date.now() / 1000);

  const statements = [
    env.DB.prepare(
      `INSERT OR IGNORE INTO openrouter_aggregate
         (fetched_at, v1_tokens, v3_tokens, combined_tokens)
       VALUES (?1, ?2, ?3, ?4)`,
    ).bind(now, v1, v3, combined),
  ];
  // Upsert per-day per-model rows. (date, app, model_id) PK means
  // re-running for the same day replaces the previous count — OR's
  // page is the source of truth and may revise yesterday's rollup.
  let daily_rows_written = 0;
  for (const [app, rows] of Object.entries(breakdownByApp)) {
    for (const { date, model_id, tokens } of rows) {
      statements.push(
        env.DB.prepare(
          `INSERT INTO openrouter_daily (date, app, model_id, tokens, scraped_at)
           VALUES (?1, ?2, ?3, ?4, ?5)
           ON CONFLICT(date, app, model_id) DO UPDATE SET
             tokens = excluded.tokens, scraped_at = excluded.scraped_at`,
        ).bind(date, app, model_id, tokens, now),
      );
      daily_rows_written += 1;
    }
  }
  await env.DB.batch(statements);

  return { v1, v3, combined, daily_rows_written };
}

// Admin endpoints are gated by a bearer token (`wrangler secret put
// ADMIN_TOKEN`). With no token configured they are disabled.
function isAdmin(request, env) {
  const token = env.ADMIN_TOKEN;
  if (!token) return false;
  const header = request.headers.get("authorization") || "";
  return header === `Bearer ${token}`;
}

export default {
  // Hourly: refresh the OR tables, then rebuild the stats snapshot so
  // /v1/stats never has to touch D1 on the request path.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        try {
          await refreshOpenRouterAggregate(env);
        } catch (e) {
          console.error("scheduled OR refresh failed:", e);
        }
        try {
          await recomputeStats(env);
        } catch (e) {
          console.error("scheduled stats recompute failed:", e);
        }
      })(),
    );
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return json({ ok: true });
    }

    if (url.pathname === "/v1/beacon" && request.method === "POST") {
      try {
        return await handleBeacon(request, env);
      } catch (e) {
        console.error("beacon failed:", e);
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    if (url.pathname === "/v1/install-event" && request.method === "POST") {
      try {
        return await handleInstallEvent(request, env);
      } catch (e) {
        console.error("install-event failed:", e);
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    if (url.pathname === "/v1/stats" && request.method === "GET") {
      // Three layers, cheapest first: the edge Cache API (5 min), the
      // KV snapshot the cron writes hourly, and only if KV is empty
      // (first deploy) a direct compute that also seeds KV.
      const cache = caches.default;
      const CACHE_KEY = new Request("https://api.free-ride.xyz/__stats-cache");
      const serve = (body, marker) =>
        new Response(body, {
          headers: {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "cache-control": "no-store",
            "x-freeride-stats": marker,
          },
        });
      const hit = await cache.match(CACHE_KEY);
      if (hit) return serve(hit.body, "cached");

      let body = await env.STATS.get(STATS_KV_KEY);
      let marker = "kv";
      if (!body) {
        try {
          body = JSON.stringify(await recomputeStats(env));
          marker = "computed";
        } catch (e) {
          console.error("stats compute failed with empty KV:", e);
          return json({ ok: false, error: "internal" }, 500);
        }
      }
      const put = cache.put(
        CACHE_KEY,
        new Response(body, {
          headers: {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "cache-control": "s-maxage=300",
          },
        }),
      );
      if (ctx?.waitUntil) ctx.waitUntil(put);
      else await put;
      return serve(body, marker);
    }

    if (url.pathname.startsWith("/v1/_admin/") && request.method === "POST") {
      if (!isAdmin(request, env)) {
        return json({ ok: false, error: "unauthorized" }, 401);
      }
      try {
        if (url.pathname === "/v1/_admin/refresh-openrouter") {
          const result = await refreshOpenRouterAggregate(env);
          const stats = await recomputeStats(env);
          return json({ ok: true, ...result, stats_as_of: stats.as_of });
        }
        if (url.pathname === "/v1/_admin/recompute-stats") {
          const stats = await recomputeStats(env);
          return json({ ok: true, stats });
        }
      } catch (e) {
        console.error("admin action failed:", e);
        return json({ ok: false, error: "admin_failed" }, 500);
      }
      return json({ ok: false, error: "not_found" }, 404);
    }

    if (url.pathname === "/install.sh" && request.method === "GET") {
      return new Response(INSTALL_SH, {
        status: 200,
        headers: { "content-type": "text/x-sh; charset=utf-8" },
      });
    }

    if (url.pathname === "/ridex.sh" && request.method === "GET") {
      return new Response(RIDEX_SH, {
        status: 200,
        headers: { "content-type": "text/x-sh; charset=utf-8" },
      });
    }

    if (url.pathname === "/install.ps1" && request.method === "GET") {
      return new Response(INSTALL_PS1, {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    if (url.pathname === "/" && request.method === "GET") {
      // Apex hosts the marketing site. The Worker only owns
      // api.free-ride.xyz; redirect bare visitors to the site.
      return Response.redirect("https://free-ride.xyz/", 301);
    }

    return json({ ok: false, error: "not_found" }, 404);
  },
};


// ---------------------------------------------------------------------------
// Embedded ridex installer (KEEP IN SYNC with install.sh in the ridex
// repo: github.com/Shaivpidadi/ridex).
// ---------------------------------------------------------------------------
const RIDEX_SH = `#!/usr/bin/env sh
# ridex installer. Run with:
#
#   curl -sSL https://api.free-ride.xyz/ridex.sh | sh
#
# What this does:
#   1. Downloads the latest ridex release tarball for this OS/arch from
#      github.com/Shaivpidadi/ridex/releases (checksum-verified) and
#      installs \`ridex\` (launcher) + \`ridex-agent\` (binary) into
#      ~/.local/bin, plus the freeride operations skill into
#      ~/.local/share/ridex/skills/.
#   2. Installs the FreeRide gateway via the existing FreeRide
#      installer (uv tool install freeride-gateway) — ridex's models
#      all come from the local FreeRide daemon on 127.0.0.1:11343.
#   3. Runs \`ridex doctor\`.
#
# Env knobs:
#   RIDEX_REF=ridex-v0.1.0   install a specific release tag
#   RIDEX_SKIP_GATEWAY=1     skip the FreeRide gateway install
#   FREERIDE_REF / FREERIDE_TELEMETRY pass through to the gateway installer

set -e

REPO="Shaivpidadi/ridex"
BIN_DIR="$HOME/.local/bin"
SHARE_DIR="$HOME/.local/share/ridex"

print() { printf '%s\\n' "$*"; }
err() { printf 'error: %s\\n' "$*" >&2; exit 1; }

print "ridex installer"
print ""

# ── OS / arch → release asset name ─────────────────────────────────
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
    Darwin) os_tag="macos" ;;
    Linux)  os_tag="linux" ;;
    *) err "ridex is not yet supported on $os (macOS and Linux only for now)." ;;
esac
case "$arch" in
    arm64|aarch64) arch_tag="aarch64" ;;
    x86_64|amd64)  arch_tag="x86_64" ;;
    *) err "unsupported architecture: $arch" ;;
esac
asset="ridex-\${os_tag}-\${arch_tag}.tar.gz"

# ── resolve the release tag ────────────────────────────────────────
if [ -n "\${RIDEX_REF:-}" ]; then
    tag="$RIDEX_REF"
else
    # Latest release whose tag starts with ridex-v (the repo is a fork
    # of vercel-labs/fx; upstream-style tags are ignored).
    tag="$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=30" \\
        | grep -o '"tag_name": *"ridex-v[^"]*"' | head -1 | cut -d'"' -f4)"
    [ -n "$tag" ] || err "could not find a ridex-v* release on github.com/$REPO"
fi
print "Installing $tag ($asset)..."

# ── download + verify ──────────────────────────────────────────────
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
base="https://github.com/$REPO/releases/download/$tag"
curl -fsSL -o "$tmp/$asset" "$base/$asset" || err "download failed: $base/$asset"
if curl -fsSL -o "$tmp/$asset.sha256" "$base/$asset.sha256" 2>/dev/null; then
    (
        cd "$tmp"
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum -c "$asset.sha256" >/dev/null
        else
            expected="$(cut -d' ' -f1 "$asset.sha256")"
            actual="$(shasum -a 256 "$asset" | cut -d' ' -f1)"
            [ "$expected" = "$actual" ]
        fi
    ) || err "checksum verification failed for $asset"
else
    print "warning: no checksum published for $asset; skipping verification"
fi

# ── install ────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR" "$SHARE_DIR"
tar -xzf "$tmp/$asset" -C "$tmp"
install -m 755 "$tmp/ridex-agent" "$BIN_DIR/ridex-agent"
install -m 755 "$tmp/ridex" "$BIN_DIR/ridex"
rm -rf "$SHARE_DIR/skills"
cp -R "$tmp/skills" "$SHARE_DIR/skills"
print "Installed ridex + ridex-agent to $BIN_DIR"

# ── FreeRide gateway ───────────────────────────────────────────────
if [ "\${RIDEX_SKIP_GATEWAY:-0}" = "1" ]; then
    print "Skipping FreeRide gateway install (RIDEX_SKIP_GATEWAY=1)."
else
    print ""
    print "Installing the FreeRide gateway (ridex's model backend)..."
    curl -sSL https://api.free-ride.xyz/install.sh | sh
fi

# ── PATH + verify ──────────────────────────────────────────────────
print ""
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        print "Note: $BIN_DIR is not on your PATH yet. Run:"
        print "  export PATH=\\"\\$HOME/.local/bin:\\$PATH\\""
        print "Or add that line to your ~/.zshrc / ~/.bashrc."
        ;;
esac

print "Checking the install..."
"$BIN_DIR/ridex" doctor || true

print ""
print "Done. Try:  ridex ask \\"reply with the single word pong\\""
`;

// ---------------------------------------------------------------------------
// Embedded install.sh (KEEP IN SYNC with /install.sh in the repo root).
// ---------------------------------------------------------------------------
const INSTALL_SH = `#!/usr/bin/env sh
# FreeRide installer. Run with:
#
#   curl -sSL https://api.free-ride.xyz/install.sh | sh
#
# What this does:
#   1. Installs uv (Astral's Python package manager) if not already.
#   2. Uses 'uv tool install' to put freeride-gateway in an isolated
#      venv and symlink the freeride binary into ~/.local/bin (which
#      uv puts on PATH).
#   3. Verifies freeride --version works.

set -e

print() { printf '%s\\n' "$*"; }
err() { printf 'error: %s\\n' "$*" >&2; exit 1; }

print "FreeRide installer"
print ""

if ! command -v uv >/dev/null 2>&1; then
    print "uv not found — installing it first..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        err "Need either curl or wget to install uv."
    fi

    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.local/bin/env"
    fi

    if ! command -v uv >/dev/null 2>&1; then
        for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
            if [ -x "$cand" ]; then
                PATH="$(dirname "$cand"):$PATH"
                export PATH
                break
            fi
        done
    fi

    if ! command -v uv >/dev/null 2>&1; then
        err "uv installed but not on PATH. Restart your shell and re-run, or run: export PATH=\\"\\$HOME/.local/bin:\\$PATH\\""
    fi
fi

print ""
# FREERIDE_REF lets early adopters install bleeding-edge from a git
# branch/tag/sha rather than the latest PyPI release. Useful when PyPI
# is behind main (e.g. a feature merged but not yet tagged).
#   FREERIDE_REF=main      curl -sSL .../install.sh | sh
#   FREERIDE_REF=v0.5.0a1  curl -sSL .../install.sh | sh
if [ -n "\${FREERIDE_REF:-}" ]; then
    print "Installing freeride-gateway from git ref: \$FREERIDE_REF"
    uv tool install --prerelease=allow --reinstall \\
        "git+https://github.com/Shaivpidadi/FreeRideV3.git@\$FREERIDE_REF"
else
    print "Installing freeride-gateway..."
    uv tool install --prerelease=allow freeride-gateway
fi

print ""
print "Verifying..."
if command -v freeride >/dev/null 2>&1; then
    freeride --version
elif [ -x "$HOME/.local/bin/freeride" ]; then
    "$HOME/.local/bin/freeride" --version
    print ""
    print "Note: $HOME/.local/bin is not on your PATH yet. Run:"
    print "  export PATH=\\"\\$HOME/.local/bin:\\$PATH\\""
    print "Or add that line to your ~/.zshrc / ~/.bashrc."
else
    err "Install completed but the freeride binary couldn't be located. Try restarting your shell."
fi

# ---------------------------------------------------------------------------
# Install-event beacon — fires once per installation, before the user has
# even run \`freeride serve\`. Closes the gap where the existing hourly
# beacon only sees CLIs that ran serve >1h with telemetry on.
# Best-effort: any failure is silent and never breaks the install.
# ---------------------------------------------------------------------------
if [ "\${FREERIDE_TELEMETRY:-on}" = "off" ] || [ "\${1:-}" = "--no-telemetry" ]; then
    :
else
    INSTALL_ID_FILE="\$HOME/.freeride/installation_id"
    mkdir -p "\$HOME/.freeride" 2>/dev/null || true
    if [ -s "\$INSTALL_ID_FILE" ]; then
        INSTALL_ID="\$(cat "\$INSTALL_ID_FILE" 2>/dev/null | tr -d '[:space:]')"
    else
        if command -v uuidgen >/dev/null 2>&1; then
            INSTALL_ID="\$(uuidgen | tr 'A-Z' 'a-z')"
        elif [ -r /proc/sys/kernel/random/uuid ]; then
            INSTALL_ID="\$(cat /proc/sys/kernel/random/uuid)"
        else
            INSTALL_ID="\$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || true)"
        fi
        if [ -n "\$INSTALL_ID" ]; then
            printf '%s' "\$INSTALL_ID" > "\$INSTALL_ID_FILE" 2>/dev/null || true
            chmod 600 "\$INSTALL_ID_FILE" 2>/dev/null || true
        fi
    fi
    case "\$(uname -s 2>/dev/null)" in
        Darwin) OS_KIND="darwin" ;;
        Linux)  OS_KIND="linux" ;;
        *)      OS_KIND="other" ;;
    esac
    INSTALLED_VERSION="\$(freeride --version 2>/dev/null | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+[a-zA-Z0-9.+-]*' | head -1)"
    INSTALLED_VERSION="\${INSTALLED_VERSION:-unknown}"
    if [ -n "\$INSTALL_ID" ] && command -v curl >/dev/null 2>&1; then
        curl -sS -m 5 -X POST https://api.free-ride.xyz/v1/install-event \\
            -H "content-type: application/json" \\
            -d "{\\"installation_id\\":\\"\$INSTALL_ID\\",\\"version\\":\\"\$INSTALLED_VERSION\\",\\"os\\":\\"\$OS_KIND\\",\\"install_method\\":\\"curl-sh\\"}" \\
            >/dev/null 2>&1 || true
    fi
fi

print ""
print "Done. Next:"
print "  export OPENROUTER_API_KEY=sk-or-v1-...      # get a free one at https://openrouter.ai/keys"
print "  freeride serve                              # start the gateway"
print "  freeride bind aider                         # point your favorite agent at it"
print ""
`;


// ---------------------------------------------------------------------------
// Tiny homepage. Lists the install command + project links.
// ---------------------------------------------------------------------------
const HOMEPAGE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FreeRide — free AI for everyone</title>
<style>
  body { font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 640px; margin: 4em auto; padding: 0 1.5em; color: #222; }
  h1 { font-size: 2em; margin: 0 0 0.4em; }
  h2 { margin-top: 2em; font-size: 1.15em; }
  pre { background: #f4f4f4; padding: 1em; border-radius: 6px; overflow-x: auto;
        font-size: 14px; line-height: 1.4; }
  code { background: #f4f4f4; padding: 0.1em 0.3em; border-radius: 3px; font-size: 95%; }
  a { color: #0a66c2; }
  .tagline { color: #555; }
</style>
</head>
<body>
<h1>FreeRide</h1>
<p class="tagline">Local OpenAI-compatible gateway. Free AI across providers, transparent failover, BYO keys.</p>

<h2>Install</h2>
<pre>curl -sSL https://api.free-ride.xyz/install.sh | sh</pre>

<h2>Use</h2>
<pre>export OPENROUTER_API_KEY=sk-or-v1-...
freeride serve
freeride bind aider     # or hermes, continue, openclaw</pre>

<h2>Links</h2>
<ul>
  <li><a href="https://github.com/Shaivpidadi/FreeRideV3">GitHub repo</a></li>
  <li><a href="https://pypi.org/project/freeride-gateway/">PyPI: freeride-gateway</a></li>
  <li><a href="/v1/stats">/v1/stats</a> — public usage counters (opt-in telemetry)</li>
</ul>
</body>
</html>
`;

// ---------------------------------------------------------------------------
// Embedded install.ps1 (KEEP IN SYNC with /install.ps1 in the repo root).
// ---------------------------------------------------------------------------
const INSTALL_PS1 = `# FreeRide installer for Windows. Run with:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://api.free-ride.xyz/install.ps1 | iex"
#
# What this does:
#   1. Installs \`uv\` (Astral's Python package manager) if it isn't already.
#   2. Uses \`uv tool install\` to install freeride-gateway into an isolated
#      venv and put the \`freeride.exe\` binary on PATH.
#   3. Verifies \`freeride --version\` works.
#
# Mirror of the POSIX \`install.sh\` — same install pattern as the Astral/uv
# Windows installer.

$ErrorActionPreference = "Stop"

function Print($msg) {
    Write-Host $msg
}

function Fail($msg) {
    Write-Host "error: $msg" -ForegroundColor Red
    exit 1
}

Print ""
Print "FreeRide installer (Windows)"
Print ""

# 1. Make sure we have uv. If not, install it via the official one-liner.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Print "uv (Python package manager) not found - installing it first..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Fail "Failed to install uv: $_"
    }

    # uv installs to %USERPROFILE%\\.local\\bin on Windows; load it onto PATH for this session.
    $uvBin = Join-Path $env:USERPROFILE ".local\\bin"
    if (Test-Path (Join-Path $uvBin "uv.exe")) {
        $env:Path = "$uvBin;" + $env:Path
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Fail "uv installed but not on PATH. Open a new PowerShell window and re-run this installer."
    }
}

Print ""
Print "Installing freeride-gateway..."
# --prerelease=allow because we ship 0.3.0a* alphas pre-stable; once 0.3.0
# final lands you can drop this flag and it'll still pick up the latest.
uv tool install --prerelease=allow freeride-gateway
if ($LASTEXITCODE -ne 0) {
    Fail "uv tool install failed (exit $LASTEXITCODE)"
}

Print ""
Print "Verifying..."
$freeride = Get-Command freeride -ErrorAction SilentlyContinue
if ($freeride) {
    & $freeride.Source --version
} else {
    $candidate = Join-Path $env:USERPROFILE ".local\\bin\\freeride.exe"
    if (Test-Path $candidate) {
        & $candidate --version
        Print ""
        Print "Note: $($env:USERPROFILE)\\.local\\bin is not on your PATH yet. Run:"
        Print "  \`$env:Path = \`"$($env:USERPROFILE)\\.local\\bin;\`" + \`$env:Path"
        Print "Or add it permanently via System Properties -> Environment Variables."
    } else {
        Fail "Install completed but the freeride binary couldn't be located. Open a new PowerShell window and try again."
    }
}

# ---------------------------------------------------------------------------
# Install-event beacon — fires once per installation, before the user has
# even run \`freeride serve\`. Best-effort: any failure is silent and never
# breaks the install.
# ---------------------------------------------------------------------------
$telemetryDisabled = (\$env:FREERIDE_TELEMETRY -eq "off")
if (-not \$telemetryDisabled -and (\$args -contains "-NoTelemetry" -or \$args -contains "--no-telemetry")) {
    \$telemetryDisabled = \$true
}
if (-not \$telemetryDisabled) {
    try {
        \$freerideDir = Join-Path \$env:USERPROFILE ".freeride"
        \$installIdFile = Join-Path \$freerideDir "installation_id"
        if (-not (Test-Path \$freerideDir)) {
            New-Item -ItemType Directory -Path \$freerideDir -Force | Out-Null
        }
        if (Test-Path \$installIdFile) {
            \$installId = (Get-Content \$installIdFile -ErrorAction SilentlyContinue).Trim()
        }
        if (-not \$installId) {
            \$installId = ([guid]::NewGuid().ToString().ToLower())
            Set-Content -Path \$installIdFile -Value \$installId -NoNewline -ErrorAction SilentlyContinue
        }
        \$installedVersion = "unknown"
        try {
            \$verLine = (& freeride --version 2>\$null) -join " "
            if (\$verLine -match '(\\d+\\.\\d+\\.\\d+[a-zA-Z0-9.+-]*)') {
                \$installedVersion = \$Matches[1]
            }
        } catch { }
        \$payload = @{
            installation_id = \$installId
            version         = \$installedVersion
            os              = "windows"
            install_method  = "powershell"
        } | ConvertTo-Json -Compress
        Invoke-RestMethod \`
            -Uri "https://api.free-ride.xyz/v1/install-event" \`
            -Method POST \`
            -ContentType "application/json" \`
            -Body \$payload \`
            -TimeoutSec 5 \`
            -ErrorAction SilentlyContinue | Out-Null
    } catch { }
}

Print ""
Print "Done. Next:"
Print "  \`$env:OPENROUTER_API_KEY = 'sk-or-v1-...'   # get a free one at https://openrouter.ai/keys"
Print "  freeride serve                              # start the gateway"
Print "  freeride bind continue                      # or aider / hermes / openclaw"
Print ""
`;
