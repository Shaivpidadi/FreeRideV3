// FreeRide site + telemetry beacon receiver — Cloudflare Worker +
// Neon Postgres.
//
// Routes (no auth; counters and installer are public-by-design):
//   GET  /             — 301 → marketing site at https://free-ride.xyz/
//   GET  /install.sh   — the POSIX (macOS / Linux) installer
//   GET  /install.ps1  — the PowerShell (Windows) installer
//   POST /v1/beacon    — accept a beacon, write a row to `beacons`.
//   GET  /v1/stats     — return aggregate counters across all beacons.
//   GET  /health       — `{ok: true}` for monitoring.
//
// The worker explicitly does NOT log or store IPs / hostnames /
// `cf-connecting-ip`. Inputs we accept are exactly the public spec
// (the design plan); anything else is dropped.
//
// Storage is Neon Postgres, reached over the HTTP driver bundled by
// wrangler. The connection string lives in env.DATABASE_URL — a
// Worker secret in production (`wrangler secret put DATABASE_URL`)
// and `.dev.vars` for `wrangler dev`. ``getSql(env)`` lazily binds
// once per cold start.
//
// Migrated from Cloudflare D1 on 2026-05-28. Schema lives in
// ./schema.pg.sql; the old D1 ./schema.sql is kept as historical
// reference but no longer applied.
//
// The installer scripts are embedded as INSTALL_SH and INSTALL_PS1
// below — KEEP IN SYNC with /install.sh and /install.ps1 at the repo
// root by hand. The repo files are the source of truth; these are
// their public-facing copies.

import { neon } from "@neondatabase/serverless";

const ALLOWED_OS = new Set(["darwin", "linux", "windows", "other"]);

// Lazily cache the Neon HTTP client per isolate. Cold starts pay the
// `neon(...)` cost; subsequent requests on the same isolate reuse the
// client. The client itself is stateless — every query is a fresh
// HTTPS request to Neon's pooler — so reusing is purely an
// allocation win.
let _sqlClient = null;
let _sqlClientKey = null;
function getSql(env) {
  if (!env.DATABASE_URL) {
    throw new Error("DATABASE_URL not configured");
  }
  if (_sqlClient && _sqlClientKey === env.DATABASE_URL) return _sqlClient;
  _sqlClient = neon(env.DATABASE_URL);
  _sqlClientKey = env.DATABASE_URL;
  return _sqlClient;
}

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
      // Skip the Cloudflare edge cache. /v1/stats is computed from a
      // moving target (beacons arrive continuously, the cron upserts
      // openrouter_* hourly), and any TTL > 0 will make the response
      // drift behind the underlying DB. The default Cloudflare edge
      // policy treats unspecified Cache-Control as cacheable, which
      // was burning us at the URL level.
      "cache-control": "no-store",
    },
  });

function clampInt(value, max = 1_000_000_000) {
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

  // ON CONFLICT DO NOTHING: re-running the installer is idempotent.
  // First install timestamp wins; we don't rewrite version on
  // re-install (re-install would be a separate event type if we
  // ever need it).
  const sql = getSql(env);
  await sql`
    INSERT INTO install_events
      (installation_id, version, os, install_method, installed_at)
    VALUES (${installation_id}, ${version}, ${os}, ${install_method},
            ${Math.floor(Date.now() / 1000)})
    ON CONFLICT (installation_id) DO NOTHING
  `;

  return json({ ok: true });
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

  const sql = getSql(env);
  await sql`
    INSERT INTO beacons
      (installation_id, version, os,
       tokens_served, input_tokens, output_tokens,
       request_count, providers_active, uptime_hours, received_at)
    VALUES (${installation_id}, ${version}, ${os},
            ${tokens_served}, ${input_tokens}, ${output_tokens},
            ${request_count}, ${JSON.stringify(providers_active)}::jsonb,
            ${uptime_hours}, ${Math.floor(Date.now() / 1000)})
  `;

  return json({ ok: true });
}

async function handleStats(env) {
  const sql = getSql(env);
  const nowSec = Math.floor(Date.now() / 1000);
  const day24Ago = nowSec - 24 * 3600;
  const day7Ago = nowSec - 7 * 24 * 3600;
  const day30Ago = nowSec - 30 * 24 * 3600;

  // Postgres counts come back as BIGINT, which the driver returns as
  // strings to avoid silent JS-number precision loss. We re-cast to
  // number here because every value we surface fits in 2^53 (token
  // totals are in the ~billions). If usage hits the 9 quadrillion
  // mark this needs to switch to BigInt-aware JSON serialization.
  const toNum = (v) => (v == null ? 0 : Number(v));

  // ─── beacons ──────────────────────────────────────────────────
  // Beacons ship CUMULATIVE counters: every hourly heartbeat carries
  // that install's running total from the start of its lifetime.
  // Summing across rows would over-count by ~(beacons-per-install)×.
  // The right aggregate is the LATEST row per installation_id, then
  // sum across installs. ``DISTINCT ON (installation_id) ... ORDER BY
  // installation_id, received_at DESC`` is Postgres's native form.
  const [all] = await sql`
    WITH latest AS (
      SELECT DISTINCT ON (installation_id)
        installation_id,
        tokens_served, input_tokens, output_tokens,
        request_count
      FROM beacons
      ORDER BY installation_id, received_at DESC
    )
    SELECT
      COUNT(*) AS installations,
      COALESCE(SUM(tokens_served), 0) AS tokens_served,
      COALESCE(SUM(input_tokens), 0)  AS input_tokens,
      COALESCE(SUM(output_tokens), 0) AS output_tokens,
      COALESCE(SUM(request_count), 0) AS request_count
    FROM latest
  `;

  // 24h breakdown: pick the latest beacon for each install within
  // the window. Installs that haven't pinged in 24h drop out.
  const [day] = await sql`
    WITH latest_24h AS (
      SELECT DISTINCT ON (installation_id)
        installation_id,
        tokens_served, input_tokens, output_tokens,
        request_count
      FROM beacons
      WHERE received_at > ${day24Ago}
      ORDER BY installation_id, received_at DESC
    )
    SELECT
      COUNT(*) AS installations_24h,
      COALESCE(SUM(tokens_served), 0) AS tokens_served_24h,
      COALESCE(SUM(input_tokens), 0)  AS input_tokens_24h,
      COALESCE(SUM(output_tokens), 0) AS output_tokens_24h,
      COALESCE(SUM(request_count), 0) AS request_count_24h
    FROM latest_24h
  `;

  // ─── openrouter aggregate (latest snapshot) ───────────────────
  const orRows = await sql`
    SELECT v1_tokens, v3_tokens, combined_tokens, fetched_at
    FROM openrouter_aggregate
    ORDER BY fetched_at DESC
    LIMIT 1
  `;
  const or = orRows[0] ?? null;

  // ─── openrouter daily rollups ─────────────────────────────────
  // Date strings are ``YYYY-MM-DD``; CURRENT_DATE minus an interval
  // yields a DATE which `to_char(... 'YYYY-MM-DD')` re-textifies for
  // the lexical comparison against `openrouter_daily.date`.
  const last7d = await sql`
    SELECT date,
           SUM(tokens) AS tokens,
           COUNT(DISTINCT model_id) AS models_count
    FROM openrouter_daily
    WHERE date >= to_char(CURRENT_DATE - INTERVAL '7 days', 'YYYY-MM-DD')
    GROUP BY date
    ORDER BY date DESC
  `;

  const topModels = await sql`
    SELECT model_id,
           SUM(tokens) AS tokens
    FROM openrouter_daily
    WHERE date >= to_char(CURRENT_DATE - INTERVAL '30 days', 'YYYY-MM-DD')
    GROUP BY model_id
    ORDER BY tokens DESC
    LIMIT 10
  `;

  // ─── openrouter lifetime ──────────────────────────────────────
  const [lifetime] = await sql`
    SELECT
      SUM(tokens) AS combined_tokens,
      SUM(CASE WHEN app = 'v1' THEN tokens ELSE 0 END) AS v1_tokens,
      SUM(CASE WHEN app = 'v3' THEN tokens ELSE 0 END) AS v3_tokens,
      MIN(date) AS since,
      MAX(date) AS through
    FROM openrouter_daily
  `;

  // ─── install velocity ─────────────────────────────────────────
  const [installs] = await sql`
    SELECT
      COUNT(*) AS total,
      COUNT(CASE WHEN installed_at > ${day24Ago} THEN 1 END) AS last_24h,
      COUNT(CASE WHEN installed_at > ${day7Ago}  THEN 1 END) AS last_7d,
      COUNT(CASE WHEN installed_at > ${day30Ago} THEN 1 END) AS last_30d
    FROM install_events
  `;

  return json({
    object: "stats",
    as_of: new Date().toISOString(),
    total: {
      installations: toNum(all?.installations),
      tokens_served: toNum(all?.tokens_served),
      input_tokens:  toNum(all?.input_tokens),
      output_tokens: toNum(all?.output_tokens),
      request_count: toNum(all?.request_count),
    },
    last_24h: {
      installations: toNum(day?.installations_24h),
      tokens_served: toNum(day?.tokens_served_24h),
      input_tokens:  toNum(day?.input_tokens_24h),
      output_tokens: toNum(day?.output_tokens_24h),
      request_count: toNum(day?.request_count_24h),
    },
    installs: {
      total: toNum(installs?.total),
      last_24h: toNum(installs?.last_24h),
      last_7d: toNum(installs?.last_7d),
      last_30d: toNum(installs?.last_30d),
    },
    openrouter_30d: or
      ? {
          v1_tokens: toNum(or.v1_tokens),
          v3_tokens: toNum(or.v3_tokens),
          combined_tokens: toNum(or.combined_tokens),
          fetched_at: toNum(or.fetched_at),
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
      last_7d: last7d.map((r) => ({
        date: r.date,
        tokens: toNum(r.tokens),
        models_count: toNum(r.models_count),
      })),
      top_models_30d: topModels.map((r) => ({
        model_id: r.model_id,
        tokens: toNum(r.tokens),
      })),
    },
  });
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
//
// Fired by the [triggers] crons entry in wrangler.toml. Idempotent —
// each run inserts one row in openrouter_aggregate. No history pruning
// (rows are tiny; ~50 bytes × ~4/day × ~365/year ≈ 73 KB/year).

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
// it by app slug. We only emit entries with positive token counts —
// OR sometimes includes models with zero, no point storing those.
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
  const sql = getSql(env);
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
      // outage doesn't reset the displayed number to zero. Daily
      // breakdown skipped on this slug — the previously stored rows
      // remain untouched, so the recent days we already have keep
      // serving /v1/stats.
      //
      // Column name has to be interpolated (not parameterized) so
      // we whitelist it against the OR_APP_URLS slugs first. ``slug``
      // is one of {'v1','v3'} per the array literal above.
      const col = slug === "v1" ? "v1_tokens" : "v3_tokens";
      const prev = await sql.query(
        `SELECT ${col} AS t FROM openrouter_aggregate
         ORDER BY fetched_at DESC LIMIT 1`,
      );
      results[slug] = Number(prev?.[0]?.t ?? 0);
      breakdownByApp[slug] = [];
    }
  }
  const v1 = results.v1 ?? 0;
  const v3 = results.v3 ?? 0;
  const combined = v1 + v3;
  const now = Math.floor(Date.now() / 1000);

  await sql`
    INSERT INTO openrouter_aggregate
      (fetched_at, v1_tokens, v3_tokens, combined_tokens)
    VALUES (${now}, ${v1}, ${v3}, ${combined})
    ON CONFLICT (fetched_at) DO NOTHING
  `;

  // Upsert per-day per-model rows. (date, app, model_id) PK means
  // re-running for the same day replaces the previous tokens count
  // — that's what we want, since OR's page is the source of truth
  // and may revise yesterday's rollup mid-day.
  //
  // D1 had ``.batch()`` for a single round-trip; the Neon HTTP
  // driver doesn't. Sequential ``await`` per row pays one HTTPS
  // round-trip each, so we batch via UNNEST instead — one query
  // total, no matter how many models OR returns. The arrays are
  // parallel: index i across all four arrays is one upsert.
  const dates = [];
  const apps = [];
  const modelIds = [];
  const tokensArr = [];
  for (const [app, rows] of Object.entries(breakdownByApp)) {
    for (const { date, model_id, tokens } of rows) {
      dates.push(date);
      apps.push(app);
      modelIds.push(model_id);
      tokensArr.push(tokens);
    }
  }
  let daily_rows_written = 0;
  if (dates.length > 0) {
    await sql`
      INSERT INTO openrouter_daily (date, app, model_id, tokens, scraped_at)
      SELECT date, app, model_id, tokens, ${now}::bigint AS scraped_at
      FROM UNNEST(
        ${dates}::text[],
        ${apps}::text[],
        ${modelIds}::text[],
        ${tokensArr}::bigint[]
      ) AS t(date, app, model_id, tokens)
      ON CONFLICT (date, app, model_id) DO UPDATE SET
        tokens = EXCLUDED.tokens,
        scraped_at = EXCLUDED.scraped_at
    `;
    daily_rows_written = dates.length;
  }

  return { v1, v3, combined, daily_rows_written };
}

export default {
  // Cron-triggered: refresh the OR aggregate table every 6 hours per
  // wrangler.toml [triggers].
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      refreshOpenRouterAggregate(env).catch((e) =>
        console.error("scheduled refresh failed:", e),
      ),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return json({ ok: true });
    }

    if (url.pathname === "/v1/beacon" && request.method === "POST") {
      try {
        return await handleBeacon(request, env);
      } catch (e) {
        // Database / unexpected errors. Don't leak details.
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    if (url.pathname === "/v1/install-event" && request.method === "POST") {
      try {
        return await handleInstallEvent(request, env);
      } catch (e) {
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    if (url.pathname === "/v1/stats" && request.method === "GET") {
      try {
        return await handleStats(env);
      } catch (e) {
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    // Manual trigger for the OR aggregate refresh — useful for the
    // first run (cron hasn't fired yet) or after a deploy when you
    // want fresh numbers immediately. Public endpoint; idempotent
    // and cheap (one HTTP fetch per app, one D1 INSERT). Returns the
    // values just written.
    if (
      url.pathname === "/v1/_admin/refresh-openrouter" &&
      request.method === "POST"
    ) {
      try {
        const result = await refreshOpenRouterAggregate(env);
        return json({ ok: true, ...result });
      } catch (e) {
        console.error("manual refresh failed:", e);
        return json({ ok: false, error: "refresh_failed" }, 500);
      }
    }

    if (url.pathname === "/install.sh" && request.method === "GET") {
      return new Response(INSTALL_SH, {
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
      // Apex hosts the marketing site (Vercel). The Worker only owns
      // api.free-ride.xyz now; redirect bare api.free-ride.xyz/ visitors
      // up to the marketing site so they don't get a stale terminal page.
      return Response.redirect("https://free-ride.xyz/", 301);
    }

    return json({ ok: false, error: "not_found" }, 404);
  },
};


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
