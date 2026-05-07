// FreeRide site + telemetry beacon receiver — Cloudflare Worker + D1.
//
// Routes (no auth; counters and installer are public-by-design):
//   GET  /             — minimal HTML homepage
//   GET  /install.sh   — the curl|sh installer script
//   POST /v1/beacon    — accept a beacon, write a row to `beacons`.
//   GET  /v1/stats     — return aggregate counters across all beacons.
//   GET  /health       — `{ok: true}` for monitoring.
//
// The worker explicitly does NOT log or store IPs / hostnames /
// `cf-connecting-ip`. Inputs we accept are exactly the public spec
// (PLAN_GATEWAY.md §14); anything else is dropped.
//
// The install.sh content is embedded in INSTALL_SH below — keep it
// in sync with /install.sh at the repo root by hand. The repo file is
// the source of truth; this is its public-facing copy.

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

    if (url.pathname === "/install.sh" && request.method === "GET") {
      return new Response(INSTALL_SH, {
        status: 200,
        headers: { "content-type": "text/x-sh; charset=utf-8" },
      });
    }

    if (url.pathname === "/" && request.method === "GET") {
      return new Response(HOMEPAGE_HTML, {
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
      });
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
#   curl -sSL https://free-ride.xyz/install.sh | sh
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
print "Installing freeride-gateway..."
uv tool install --prerelease=allow freeride-gateway

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
<pre>curl -sSL https://free-ride.xyz/install.sh | sh</pre>

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
