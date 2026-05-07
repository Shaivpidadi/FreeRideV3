// FreeRide telemetry beacon receiver — Cloudflare Worker + D1.
//
// Three routes, no auth (counters are public-by-design):
//   POST /v1/beacon  — accept a beacon, write a row to `beacons`.
//   GET  /v1/stats   — return aggregate counters across all beacons.
//   GET  /health     — `{ok: true}` for monitoring.
//
// The worker explicitly does NOT log or store IPs / hostnames /
// `cf-connecting-ip`. Inputs we accept are exactly the public spec
// (PLAN_GATEWAY.md §14); anything else is dropped.

const ALLOWED_OS = new Set(["darwin", "linux", "windows", "other"]);

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
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
  const tokens_served = clampInt(body.tokens_served);
  const request_count = clampInt(body.request_count);
  const uptime_hours = clampInt(body.uptime_hours, 24 * 365 * 10); // <= 10y
  const providers_active = sanitizeProviders(body.providers_active);

  await env.DB.prepare(
    `INSERT INTO beacons
      (installation_id, version, os, tokens_served, request_count,
       providers_active, uptime_hours, received_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(
      installation_id,
      version,
      os,
      tokens_served,
      request_count,
      JSON.stringify(providers_active),
      uptime_hours,
      Math.floor(Date.now() / 1000),
    )
    .run();

  return json({ ok: true });
}

async function handleStats(env) {
  const all = await env.DB.prepare(
    `SELECT
       COUNT(DISTINCT installation_id) AS installations,
       COALESCE(SUM(tokens_served), 0) AS tokens_served,
       COALESCE(SUM(request_count), 0) AS request_count
     FROM beacons`,
  ).first();

  const day = await env.DB.prepare(
    `SELECT
       COUNT(DISTINCT installation_id) AS installations_24h,
       COALESCE(SUM(tokens_served), 0) AS tokens_served_24h,
       COALESCE(SUM(request_count), 0) AS request_count_24h
     FROM beacons
     WHERE received_at > ?`,
  )
    .bind(Math.floor(Date.now() / 1000) - 24 * 3600)
    .first();

  return json({
    object: "stats",
    as_of: new Date().toISOString(),
    total: {
      installations: all?.installations ?? 0,
      tokens_served: all?.tokens_served ?? 0,
      request_count: all?.request_count ?? 0,
    },
    last_24h: {
      installations: day?.installations_24h ?? 0,
      tokens_served: day?.tokens_served_24h ?? 0,
      request_count: day?.request_count_24h ?? 0,
    },
  });
}

export default {
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

    if (url.pathname === "/v1/stats" && request.method === "GET") {
      try {
        return await handleStats(env);
      } catch (e) {
        return json({ ok: false, error: "internal" }, 500);
      }
    }

    return json({ ok: false, error: "not_found" }, 404);
  },
};
