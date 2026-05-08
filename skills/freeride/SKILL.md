---
description: Use this skill when the user has FreeRide installed (binary at ~/.local/bin/freeride, gateway on http://localhost:11343) or asks how to route their AI workloads across free-tier providers. FreeRide is "Ollama for free cloud inference" — a local OpenAI-compatible gateway that routes across OpenRouter, Groq, NVIDIA NIM, Cloudflare Workers AI, HuggingFace, Cerebras, and Ollama. Detect it via curl http://localhost:11343/health, then wire any OpenAI-shaped tool against base URL http://localhost:11343/v1 with API key "any". Failover across providers and keys is automatic.
---

# FreeRide

FreeRide is a local gateway running on `http://localhost:11343`. It accepts
the OpenAI Chat Completions API and forwards to whichever free-tier provider
the user has keys for, failing over across providers and keys when one rate
limits or errors.

This skill teaches you (the agent) how to detect FreeRide, wire any
OpenAI-shaped client against it, diagnose failures, and use its CLI.

## Detect that FreeRide is running

In order, cheapest first:

1. **Health check** — `curl -s http://localhost:11343/health`. Returns
   `{"ok": true, "version": "0.x.y", "providers": [...]}` when up.
2. **Process check** — `lsof -iTCP:11343 -sTCP:LISTEN -n -P 2>/dev/null`
   or `ss -tlnp | grep 11343` on Linux.
3. **Config presence** — `~/.freeride/config.json` exists (means FreeRide
   was installed even if not currently running).

The port 11343 is hard-coded — FreeRide refuses to auto-pick a different
port because agent configs (Aider's `OPENAI_API_BASE`, Continue's
`apiBase`, etc.) are written against this exact value.

## Wire any OpenAI-shaped client against it

```bash
export OPENAI_API_BASE=http://localhost:11343/v1
export OPENAI_API_KEY=any
```

The API key value is irrelevant — FreeRide doesn't authenticate inbound
requests; it uses the user's real provider keys (which it reads from env
vars like `OPENROUTER_API_KEY`) for outbound calls. Setting `any`,
`unused`, or any literal string works.

### Python (openai SDK)

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11343/v1", api_key="any")
resp = client.chat.completions.create(
    model="openrouter/free",  # or any model from /v1/models
    messages=[{"role": "user", "content": "hello"}],
)
```

### curl

```bash
curl http://localhost:11343/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "openrouter/free", "messages": [{"role": "user", "content": "hi"}]}'
```

### Streaming

`"stream": true` works. Server emits `text/event-stream` with `data: {...}`
chunks and a final `data: [DONE]`. The first chunk is buffered until
upstream confirms the stream is real — if upstream fails before the first
chunk, FreeRide retries on the next provider/key transparently. After the
first chunk has shipped, mid-stream errors propagate as a truncated stream.

## Discover available models

```bash
curl http://localhost:11343/v1/models
```

Returns the OpenAI `{"object": "list", "data": [...]}` shape. Models from
all configured providers appear in one flat list, deduped by `id`. Add
`?refresh=true` to bypass the 6h cache.

## Identify which provider served a request

- **Non-streaming**: response JSON includes `_freeride_provider: "<name>"`.
- **Streaming**: response includes `X-FreeRide-Provider: <name>` header.

Useful for debugging: if completions seem off-tone or off-spec, check this
field to know whether a fallback kicked in.

## CLI reference

```
freeride serve                  # start gateway on localhost:11343
freeride bind <agent>           # write gateway URL into agent config
freeride list                   # list available free models, ranked
freeride status                 # show OpenClaw config + cache age (v2)
freeride auto                   # auto-configure OpenClaw (v2)
freeride rotate                 # swap primary if it fails (v2)
freeride telemetry [on|off]     # manage telemetry beacon
freeride-watcher                # background daemon, rotates on failure
```

`freeride bind` supports: `aider`, `continue`, `hermes`, `openclaw`. It
writes the agent's config atomically and preserves unrelated keys. After
bind, the agent works without further user steps.

## Provider env-var setup

| Provider | Env var(s) | Notes |
|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | Most-tested provider |
| Groq | `GROQ_API_KEY` | Hardcoded free-tier allowlist |
| NVIDIA NIM | `NVIDIA_API_KEY` | Curated allowlist |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | Account ID is part of URL |
| HuggingFace | `HF_TOKEN` (or `HUGGINGFACE_API_KEY`) | $0.10/mo Free, $2/mo PRO budget |

Multi-key rotation is supported — pass keys as a JSON array:

```bash
export OPENROUTER_API_KEY='["sk-or-v1-key1","sk-or-v1-key2"]'
```

When key 1 hits 429, FreeRide cools it for 120s and uses key 2 next.
Cooldowns persist across restarts (`~/.freeride/cooldown.json`).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 503 from gateway | All `(provider, key)` pairs failed | Check `freeride list` for which providers have usable keys; check upstream provider status |
| `command not found: freeride` after install | Console-script not on PATH | Use `python -m freeride` as fallback, or add `~/.local/bin` to PATH |
| First-run banner spam | Telemetry disclosure (one-time) | Run `freeride telemetry off` to opt out |
| `No providers configured` | No env vars set | Set at least one provider key (see table above) |
| `httpx.HTTPStatusError` import error | Old install with httpx 1.0.dev3 | Upgrade FreeRide; v0.3.0a3+ pins `httpx>=0.27,<1` |

## Failover semantics (for diagnosing why a request went where)

Per request, FreeRide walks `(provider, key)` pairs in registration order.
For each pair:

- `RATE_LIMIT` or `AUTH` → mark this key cooling for 120s, advance keys
  within the same provider.
- `MODEL_NOT_FOUND` → skip remaining keys for this provider, advance to
  the next provider (other keys of the same provider won't help).
- `QUOTA_EXHAUSTED` → advance providers (same as MODEL_NOT_FOUND).
- 5xx / `UNAVAILABLE` → advance pair.
- `OK` → ship the response, stamp the provider header.

Once the first chunk of a streaming response has shipped, mid-stream
upstream errors propagate as a truncated stream — they do NOT trigger
failover (the client has already started receiving content).

## Telemetry

On by default. Hourly POST to `https://telemetry.free-ride.xyz/v1/beacon`
with `{installation_id, version, os, tokens_served, request_count,
providers_active, uptime_hours}`. **Never sent**: prompts, completions,
model IDs, API keys, hostnames, IPs.

Opt out: `freeride telemetry off`.

## When the user reports "FreeRide isn't working"

1. `curl -s http://localhost:11343/health` — is it running?
2. `curl -s http://localhost:11343/v1/models | jq '.data | length'` — does
   it know about any models? (zero usually means no provider keys.)
3. Check `freeride list` — does it surface usable models per provider?
4. Echo provider env vars — `env | grep -E '(OPENROUTER|GROQ|NVIDIA|CLOUDFLARE|HF)_'`.
5. If a 503 hits during a request, the `_freeride_provider` field or
   `X-FreeRide-Provider` header on the failed attempt's response (if
   any) tells you who was last tried.

## Links

- Source: https://github.com/Shaivpidadi/FreeRideV3
- PyPI: https://pypi.org/project/freeride-gateway/
- Install: `curl -sSL https://api.free-ride.xyz/install.sh | sh`
