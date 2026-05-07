# FreeRide

**Free AI for everyone.** A local OpenAI-compatible gateway that orchestrates free-tier inference across multiple providers — OpenRouter, NVIDIA NIM, and more — and routes around outages and rate limits transparently.

```
[any agent] ──HTTP──> [FreeRide on localhost] ──HTTPS──> OpenRouter
                                                    └──> NVIDIA NIM
                                                    └──> (more providers)
```

Point any OpenAI-compatible client at `http://localhost:11343/v1` with API key `any` and you get free AI. When one provider rate-limits or fails, FreeRide invisibly fails over to the next. Streaming, tool calls, vision, and structured outputs all pass through.

## Why

You can already get free models from OpenRouter, NIM, Groq, etc. — but each has different rate limits, different free-detection rules, and different rate-limit semantics. Hitting one's daily cap means your agent stalls until tomorrow. FreeRide unifies them behind one OpenAI-shaped endpoint and rotates across providers and keys so your agent never sees a 429.

Crucially:

- **Local-first.** The gateway runs on your machine. Your prompts and completions never touch any FreeRide-operated server.
- **Free-only by religion.** No paid fallback paths. No upsells.
- **BYO keys.** You bring your own free-tier keys for each provider; FreeRide just routes.
- **Telemetry off by default.** Optional, audit-friendly aggregate beacon (token counts, no content) — opt-in only via `freeride telemetry on`.

## Install

```bash
curl -sSL https://raw.githubusercontent.com/Shaivpidadi/FreeRideV3/main/install.sh | sh
```

That's it. `freeride` works in every terminal afterward.

The script bootstraps `uv` if you don't have it, then `uv tool install`s freeride-gateway. The binary lands at `~/.local/bin/freeride`, which uv ensures is on PATH. Same shape as the bun.sh / astral.sh/uv / aider.chat installers.

<details>
<summary>Manual install</summary>

```bash
# Option A: uv (what the installer above does)
uv tool install --prerelease=allow freeride-gateway

# Option B: pipx
pipx install --pip-args=--pre freeride-gateway

# Option C: pip + venv (works in the venv only; need to re-activate per shell)
python3 -m venv .venv && source .venv/bin/activate
pip install --pre freeride-gateway

# Option D: from source (for development)
git clone https://github.com/Shaivpidadi/FreeRideV3 && cd FreeRideV3
pip install -e .
```

PyPI distribution is `freeride-gateway`; CLI binary is `freeride`. Python ≥ 3.10.
</details>

## Quick start

### 1. Get free API keys

| Provider | Sign-up | Required env var |
|---|---|---|
| OpenRouter | https://openrouter.ai/keys | `OPENROUTER_API_KEY` |
| NVIDIA NIM | https://build.nvidia.com/explore/discover | `NVIDIA_API_KEY` |

You only need one to get started; more = better failover.

### 2. Start the gateway

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
export NVIDIA_API_KEY="nvapi-..."  # optional

freeride serve
# freeride gateway listening on http://127.0.0.1:11343
#   providers: openrouter, nvidia_nim
#   point any OpenAI-compatible agent at:
#     OPENAI_API_BASE=http://127.0.0.1:11343/v1
#     OPENAI_API_KEY=any
```

### 3. Point your agent at it

The fastest way is via a built-in binder:

```bash
freeride bind aider       # writes ~/.aider.conf.yml
freeride bind continue    # writes ~/.continue/config.yaml
freeride bind hermes      # writes ~/.hermes/cli-config.yaml
freeride bind openclaw    # writes ~/.openclaw/openclaw.json
```

Or point any OpenAI-shaped client manually:

```bash
export OPENAI_API_BASE=http://localhost:11343/v1
export OPENAI_API_KEY=any
```

That's it. Your agent now uses free AI with cross-provider failover.

### 4. (Optional) Multi-key rotation

```bash
# JSON-array form to register multiple keys per provider
export OPENROUTER_API_KEY='["sk-or-v1-key1", "sk-or-v1-key2"]'
```

When one key hits 429, FreeRide marks it cooling and uses the next on the next request. Cooldowns persist across restarts.

## How it works

### Cross-provider failover

When you call `chat/completions`, FreeRide tries providers in registration order. For each provider it walks the available (non-cooling) keys; on `RATE_LIMIT` or `AUTH` it marks the key cooling and tries the next. On `MODEL_NOT_FOUND` it advances to the next provider. Once a provider produces a successful response (or a streaming response's first chunk), FreeRide commits and returns it to the client.

The client never sees the failures — the response includes a `_freeride_provider` field (or `X-FreeRide-Provider` header on streaming responses) so you can audit which provider actually served any given request.

### Streaming

Streaming uses **buffer-first-chunk failover**: FreeRide holds the first SSE event from upstream until it confirms the stream started successfully. If upstream errors before producing the first chunk, FreeRide tries the next (provider, key) tuple. Once the first chunk has shipped to the client, mid-stream errors propagate as a truncated stream (rare in practice; documented limitation).

### Telemetry — ENABLED BY DEFAULT, opt out anytime

FreeRide ships with anonymous aggregate telemetry **enabled**. The first time you run any `freeride` command, a one-time disclosure banner prints showing exactly what gets sent.

**Sent hourly** to `https://telemetry.free-ride.xyz/v1/beacon` (silent on failure):

```json
{
  "installation_id": "uuid-v4 random per install, opaque",
  "version": "0.3.0",
  "os": "darwin",
  "tokens_served": 412034,
  "request_count": 187,
  "providers_active": ["openrouter", "nvidia_nim"],
  "uptime_hours": 8
}
```

**Never sent**: prompts, completions, model IDs, API keys, hostnames, IPs. The backend (`services/telemetry/`, this repo) explicitly does not log `cf-connecting-ip`.

To opt out:

```bash
freeride telemetry off
```

To audit the exact payload before any of it is sent:

```bash
freeride telemetry
```

If you'd rather never have any beacon attempted at all, `freeride telemetry off` is sufficient — the gateway never POSTs after that.

## Commands

| Command | What it does |
|---|---|
| `freeride serve` | Start the gateway on `localhost:11343` |
| `freeride bind <agent>` | Write the gateway URL into the agent's config (atomic; preserves unrelated keys) |
| `freeride telemetry [on\|off]` | Manage opt-in beacon (default OFF) |
| `freeride list` | List available free models, ranked (v2 behavior) |
| `freeride status` | Show current OpenClaw config + cache age (v2 behavior) |
| `freeride auto` | Auto-configure best free model for OpenClaw (v2 behavior) |
| `freeride rotate` | Live-test current primary; swap if it fails (v2 behavior) |
| `freeride-watcher` | Background daemon that probes and rotates on failure (v2 behavior) |

The v2 commands keep working for existing OpenClaw users; the new commands (`serve`, `bind`, `telemetry`) are the v3 surface.

## Supported providers

- **OpenRouter** ✅ — chat, streaming, tools, vision, structured outputs
- **NVIDIA NIM** ✅ — chat, streaming (curated free-model allowlist; `NVIDIA_NIM_FREE_MODELS_OVERRIDE` env var to expand)
- *Groq*, *Cloudflare Workers AI*, *HuggingFace Inference Providers*: Provider Protocol fits all three (see `knowledge/providers/SURVEY.md`); plugin implementations welcome.

## Supported agents

| Agent | `freeride bind <agent>` | Hot reload |
|---|---|---|
| OpenClaw | ✅ | restart needed |
| Aider | ✅ (--scope home/cwd/git) | restart needed |
| Continue | ✅ | yes |
| Hermes (NousResearch/hermes-agent) | ✅ | restart needed |
| OpenCode | extended; not yet shipped | — |

Or any other OpenAI-compatible client via `OPENAI_API_BASE` + `OPENAI_API_KEY=any`.

## Project documents

- [`knowledge/PLAN_GATEWAY.md`](knowledge/PLAN_GATEWAY.md) — design plan, decisions, telemetry spec
- [`knowledge/EXECUTION_PLAN.md`](knowledge/EXECUTION_PLAN.md) — phased execution playbook (90+ tasks)
- [`knowledge/providers/`](knowledge/providers/) — per-provider technical references
- [`knowledge/CONSUMERS.md`](knowledge/CONSUMERS.md) — per-agent bind reference
- [`knowledge/HERMES.md`](knowledge/HERMES.md) — Hermes identification + bind plan

## Contributing

The Provider Protocol is `freeride.core.provider.Provider` with `api_version = 1`. To add a new provider:

1. Implement the Protocol in `freeride/providers/<name>.py`
2. Register your class in `tests/conformance/test_provider_conformance.py`'s `CONFORMANT_PROVIDERS` list
3. Add `freeride/providers/<name>_model_metadata.py` if the catalog endpoint doesn't expose context length / capabilities

The conformance suite covers the load-bearing invariants automatically.

## License

MIT.
