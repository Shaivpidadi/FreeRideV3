// FreeRide site + telemetry beacon receiver — Cloudflare Worker + D1.
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
// The installer scripts are embedded as INSTALL_SH and INSTALL_PS1
// below — KEEP IN SYNC with /install.sh and /install.ps1 at the repo
// root by hand. The repo files are the source of truth; these are
// their public-facing copies.

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

Print ""
Print "Done. Next:"
Print "  \`$env:OPENROUTER_API_KEY = 'sk-or-v1-...'   # get a free one at https://openrouter.ai/keys"
Print "  freeride serve                              # start the gateway"
Print "  freeride bind continue                      # or aider / hermes / openclaw"
Print ""
`;
