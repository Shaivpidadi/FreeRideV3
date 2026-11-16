---
name: freeride
description: >-
  Diagnose and fix the FreeRide gateway that serves this agent's models.
  Use whenever model requests fail or stall (connection refused, HTTP 503,
  "All providers failed", "No providers have usable keys", provider
  unavailable/rate limit notices, empty model list, slow first token),
  whenever the user asks about the daemon, providers, API keys, models,
  cooldowns, or telemetry, and before touching anything under ~/.freeride
  .
---

# FreeRide operations

This agent (fx) does not talk to model vendors directly: FreeRide is its selected provider connection. Every request
goes to **FreeRide**, a local gateway daemon on `http://127.0.0.1:11343`,
which fans out across free-tier providers — OpenRouter, Google Gemini,
Mistral, SambaNova, Z.ai, Groq, NVIDIA NIM, Cloudflare Workers AI, Ollama
Cloud, HuggingFace (PRO), a long tail of smaller gateways, keyless
fallbacks (OVHcloud, BlockRun) and local Ollama — with automatic failover
when one rate-limits or errors. If model requests break,
the fault is almost always in this chain, and you can diagnose and fix it
yourself with the commands below.

```
fx (this agent) ──OpenAI chat completions──▶ FreeRide :11343 ──failover──▶ free providers
```

Architecture facts that matter for diagnosis:

- The port is hard-coded to **11343**; FreeRide refuses to auto-pick another.
- Requests walk a health-ordered chain of (provider, key) pairs. A failing
  key is put on **cooldown**: rate limit → the provider's Retry-After, else
  60s; invalid/auth-rejected key → 5 min; daily quota exhausted → 60 min.
- Model ids `freeride/coding` (the default `freeride bind fx` selects), `freeride/fast`,
  `freeride/quality`, `freeride/free`, and `auto` are routing presets, not
  real models — the gateway picks a concrete free model per request.
  Concrete ids from the model list also work verbatim.
- EVERY request carries an internal fallback LADDER: if the serving
  provider can't deliver (rate limit, no free inference right now, dead
  key, retired model — including a concretely-picked model whose only
  provider is cooling), the gateway silently retries on the next
  provider's best tool-capable model, inside the same response. An
  upstream dying mid-stream before any output also switches silently;
  after output it ends the turn as an explicit error so the agent
  retries rather than trusting a truncated answer. A single provider
  being down should therefore never surface as a failed turn — if the
  user still sees failures, EVERY ladder provider is keyless/cooling
  (check `freeride keys`).
- Streaming commits after the first token: a provider dying mid-stream
  truncates that one response (known limitation); just retry the turn.
- Inbound auth is ignored — any Bearer token works against FreeRide. The
  real provider keys live server-side in `~/.freeride/.env`.

## First move: read the gateway's own diagnosis

Always start with these two, cheapest first:

```bash
curl -sf -m 3 http://127.0.0.1:11343/health
freeride doctor
```

These read-only diagnostics are **pre-approved** — run them without
hesitation: `freeride doctor`, `freeride keys`, `freeride providers`,
`freeride telemetry`, `freeride daemon status`, `freeride --version`, and the
health probe spelled exactly `curl -sf -m 3 http://127.0.0.1:11343/health`
(also accepted: no flags, `-s`, or `-sf`, and `localhost` in place of
the IP). Run each as its OWN command, with no other arguments:
chaining (`&&`, `;`, pipes), wrapping, or adding flags forfeits the
pre-approval and triggers a permission review.

`/health` returns `{"ok": true, "version": ..., "providers": [...],
"keyed_providers": [...]}`. Read it precisely:

- **Connection refused / timeout** → the daemon isn't running (see next
  section).
- **`keyed_providers` is empty** → no provider holds a usable key right
  now: either none were configured (`freeride init`) or all are cooling
  (`freeride keys` shows which and when they return). `providers` alone
  is NOT a key signal — openrouter is always registered, keyed or not.

`freeride doctor` checks Python, PATH, `~/.freeride/`, every provider env
var, the port, and telemetry, and prints a per-check verdict with the fix
inline.

## Daemon lifecycle

`freeride daemon` manages the gateway; state lives in `~/.freeride/daemon/`:

| command                   | effect |
|---|---|
| `freeride daemon install` | register the gateway with launchd / systemd so it is always running |
| `freeride daemon start`   | start it now (clears a previous stop) |
| `freeride daemon stop`    | stop it — **sticks** via `~/.freeride/daemon/stopped` until `start` |
| `freeride daemon restart` | stop + wait for the port to free + start |
| `freeride daemon status`  | supervision mode, health, log path |

- The daemon is SUPERVISED where possible: a launchd LaunchAgent on
  macOS (`xyz.free-ride.gateway`, KeepAlive on abnormal exit) or a
  systemd user unit on Linux (`freeride.service`, Restart=on-failure) —
  a crashed daemon restarts itself within seconds. `freeride daemon status`
  shows which supervision mode is active.
- Daemon log: `~/.freeride/daemon.log` (falls back to
  `~/.freeride/autospawn.log` for gateways started by `freeride run`).
- "Connection refused" while `~/.freeride/daemon/stopped` exists means the
  user stopped it deliberately — say so and suggest `freeride daemon start`;
  do not restart it yourself without being asked to get it running.
- "port 11343 already in use" in the log → something else owns the port:
  `lsof -nP -iTCP:11343 -sTCP:LISTEN`.

## Reading failures

**Structured 503 (JSON body)** — the gateway exhausted its chain. The body
lists exactly what was tried:

```json
{"error": {"message": "All providers failed.", "tried": [
  {"provider": "openrouter", "last_error": "rate_limit", "keys_tried": 2},
  {"provider": "groq", "last_error": "auth", "keys_tried": 1}]}}
```

Act on `last_error` per provider:

- `rate_limit` — transient; keys return automatically (`freeride keys`
  shows "soonest back"). More keys per provider = more headroom.
- `auth` — that provider's key is invalid/revoked. Fix the value in
  `~/.freeride/.env` or re-run `freeride init`, then `freeride reload`.
- `quota_exhausted` — daily/monthly cap; that key is out for ~1h+. Another
  provider's key is the real fix.
- `model_not_found` — a concrete model id retired upstream; the catalog
  cache is invalidated automatically, so retry, or use `auto`/a preset.
- `unavailable` / timeouts — provider-side outage; failover already
  skipped it, so persistent failure means every configured provider is
  down or keyless.

**In-stream error** — a streamed turn can end with an `error` part and
`finishReason "error"` instead of an HTTP error; same taxonomy, read the
message.

**Empty model list** (`fx models` shows only the presets) → no keyed
provider is reachable; same fix as empty `keyed_providers`.

## Keys

- Stored ONLY in `~/.freeride/.env` (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
  `MISTRAL_API_KEY`, `SAMBANOVA_API_KEY`, `ZAI_API_KEY`, `GROQ_API_KEY`,
  `NIM_API_KEY`/`NVIDIA_API_KEY`, `CLOUDFLARE_API_TOKEN`+`CLOUDFLARE_ACCOUNT_ID`,
  `OLLAMA_CLOUD_API_KEY`, `HUGGINGFACE_API_KEY`, `CEREBRAS_API_KEY`,
  `OLLAMA_BASE_URL`, plus `<NAME>_API_KEY` for the smaller gateways).
  Numbered variants (`OPENROUTER_API_KEY_2`, …) add failover headroom on
  the same provider. `FREERIDE_KEYLESS=ovh,blockrun` enables the
  anonymous gateways when no key is configured at all.
- `freeride keys` — per-provider: how many keys, how many available vs
  cooling, and when the soonest returns.
- `freeride init` — the interactive wizard; after any `.env` edit run
  `freeride reload` (hot-reloads the registry, no restart).
- A key that "was working yesterday" and now 401s is usually stale in
  `~/.freeride/.env` while the user updated it somewhere else — verify
  against `.env`, not shell env.
- **Never print, echo, or paste key values** — not in output, not in
  commands that would display them (`cat ~/.freeride/.env` is off-limits;
  use `grep -c` / `grep -o '^[A-Z_]*'` shapes instead). Never send keys
  anywhere except `~/.freeride/.env`.

## Deeper tools

- `freeride providers` — live per-provider health/latency from the gateway.
- `freeride bench` — per-provider latency comparison.
- `freeride audit-models` — probe every catalog model, persist health
  verdicts that smart-routing uses to skip broken ids.
- `~/.freeride/cooldown.json` — cooldown state (key hashes only, safe to
  read); `~/.freeride/events.jsonl` — request/failover event log, grep by
  request id from the `X-FreeRide-Request-Id` response header.
- `freeride telemetry` — audit what the default-on aggregate beacon sends
  (tokens/provider counts only); `freeride telemetry off` disables it.
- `freeride upgrade` — update the gateway package.
- Slow first token on free tiers is normal (the gateway holds the
  connection with keepalives while providers warm up); tens of seconds of
  silence is not a hang.

## Escalation order

1. `/health` reachable? → daemon problem: log, stop-marker, port.
2. `keyed_providers` non-empty? → key problem: `freeride keys`,
   `freeride init` + `freeride reload`.
3. Requests still 503 → read the `tried` list and fix the named
   `last_error`s; check `freeride providers` for a full outage.
4. Weird model behavior (wrong model, refused id) → `fx models`,
   prefer `freeride/coding` or `auto`.
5. Bug in the gateway itself → `~/.freeride/events.jsonl` around the
   failing request id, then report with `freeride --version`.
