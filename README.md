# FreeRide

One free AI endpoint. Five providers behind it. Your agents don't need to know.

```
$ curl -sSL https://api.free-ride.xyz/install.sh | sh
$ export OPENROUTER_API_KEY=sk-or-v1-...
$ freeride serve

freeride gateway listening on http://127.0.0.1:11343
  providers: openrouter        # add more by exporting their keys
  point any OpenAI-compatible agent at:
    OPENAI_API_BASE=http://127.0.0.1:11343/v1
    OPENAI_API_KEY=any
```

That's it. Aider, Continue, OpenClaw, Hermes, the OpenAI Python SDK — anything that speaks OpenAI now speaks every free tier you have a key for.

## Demo

```
┌─ your agent ─────────┐         ┌─ freeride (localhost) ─┐         ┌─ providers ─┐
│                      │  POST   │                        │         │             │
│  chat.completions    │────────▶│  pick provider         │────────▶│  OpenRouter │ 429
│   .create(...)       │         │  pick key (not cooling)│  retry  │     ↓       │
│                      │         │  forward request       │────────▶│  Groq       │ ✓
│  ◀───────────────────│   200   │  ◀─────────────────────│         │             │
│                      │         │                        │         │  NIM, CF,   │
│                      │         │  X-FreeRide-Provider:  │         │  HF — only  │
│                      │         │   groq                 │         │  if needed  │
└──────────────────────┘         └────────────────────────┘         └─────────────┘
```

When OpenRouter rate-limits you, the next request goes to Groq. When Groq's daily token cap hits, the next goes to HuggingFace. Your agent never sees a 429.

## Why this exists

You can already get a free tier from OpenRouter. And NVIDIA. And Groq. And Cloudflare Workers AI. And HuggingFace. They all have different limits, different free-detection rules, different ways of saying "you're done for today."

So you sign up for all of them and now you've got five API keys, five SDKs, and an agent that only knows about one. FreeRide is the small thing that sits between them and pretends to be one OpenAI endpoint.

- **Local-first.** The gateway runs on your machine. Prompts and completions never touch a FreeRide server.
- **BYO keys.** Bring your own free-tier keys. FreeRide doesn't issue any.
- **Free-only.** No paid fallback. No upsell. If every provider is exhausted, the request fails — better that than a surprise bill.

## Install

**macOS / Linux:**

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://api.free-ride.xyz/install.ps1 | iex"
```

The installer bootstraps `uv` if missing, then `uv tool install`s `freeride-gateway`. Binary lands at `~/.local/bin/freeride` (Linux/macOS) or `%USERPROFILE%\.local\bin\freeride.exe` (Windows). Same shape as the bun.sh and astral.sh installers.

<details>
<summary>Or install manually</summary>

```bash
# uv (what the installer does)
uv tool install --prerelease=allow freeride-gateway

# pipx
pipx install --pip-args=--pre freeride-gateway

# pip + venv (the venv only — re-activate per shell)
python3 -m venv .venv && source .venv/bin/activate
pip install --pre freeride-gateway

# from source
git clone https://github.com/Shaivpidadi/FreeRideV3 && cd FreeRideV3
pip install -e .
```

PyPI distribution: `freeride-gateway`. CLI: `freeride`. Python ≥ 3.10.
</details>

## Get keys (any one is enough; more = better failover)

| Provider | Where | Env var |
|---|---|---|
| OpenRouter | https://openrouter.ai/keys | `OPENROUTER_API_KEY` |
| Groq | https://console.groq.com/keys | `GROQ_API_KEY` |
| NVIDIA NIM | https://build.nvidia.com | `NVIDIA_API_KEY` |
| Cloudflare Workers AI | https://dash.cloudflare.com/profile/api-tokens | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` |
| HuggingFace | https://huggingface.co/settings/tokens | `HF_TOKEN` |
| Ollama (local) | https://ollama.com/download | `OLLAMA_BASE_URL=http://localhost:11434` |

Set whichever you have, then `freeride serve`. The gateway picks them up and rotates between them.

## Wire your agent

The fastest way is a binder:

```bash
freeride bind aider       # writes ~/.aider.conf.yml
freeride bind continue    # writes ~/.continue/config.yaml
freeride bind hermes      # writes ~/.hermes/config.yaml
freeride bind openclaw    # writes ~/.openclaw/openclaw.json
```

Or set the OpenAI vars yourself:

```bash
export OPENAI_API_BASE=http://localhost:11343/v1
export OPENAI_API_KEY=any
```

Anything OpenAI-shaped works. Tested with the openai-python SDK, Aider, Continue, Hermes, OpenClaw.

## Multi-key rotation

Got several free keys for the same provider? Pass them as a JSON array:

```bash
export OPENROUTER_API_KEY='["sk-or-v1-key1","sk-or-v1-key2","sk-or-v1-key3"]'
```

When key 1 hits 429 it goes on cooldown for 120s; key 2 takes the next request. Cooldowns persist across restarts (`~/.freeride/cooldown.json`).

## How failover works

Per request, FreeRide walks `(provider, key)` pairs in order:

- `RATE_LIMIT` or `AUTH` → mark this key cooling, try the next key.
- `MODEL_NOT_FOUND` → skip this provider, try the next provider.
- Anything 5xx-ish → next pair.
- First successful response → ship it; stamp `X-FreeRide-Provider` header (or `_freeride_provider` field on JSON) so you can tell who actually served it.

Streaming uses buffer-first-chunk failover: hold the first SSE event until upstream confirms the stream is real. If it fails before the first chunk, retry. After the first chunk has shipped, mid-stream errors propagate (rare; documented).

## Telemetry

On by default. Hourly POST to `https://telemetry.free-ride.xyz/v1/beacon`:

```json
{
  "installation_id": "random-uuid-v4",
  "version": "0.3.0",
  "os": "darwin",
  "tokens_served": 412034,
  "request_count": 187,
  "providers_active": ["openrouter", "groq"],
  "uptime_hours": 8
}
```

Prompts, completions, model IDs, API keys, hostnames, IPs — never sent. The Worker doesn't log `cf-connecting-ip`. The first time you run any `freeride` command a banner prints the exact payload.

```bash
freeride telemetry off    # turn it off
freeride telemetry        # show what would be sent
```

## Embeddings

Same endpoint shape as OpenAI's `/v1/embeddings`. Failover across the
4 providers that support embeddings (Groq doesn't):

```bash
curl http://localhost:11343/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model": "text-embedding-3-small", "input": "hello world"}'
```

The same `X-FreeRide-Provider` header tells you which provider served
the embedding. Same multi-key rotation, same per-provider failover.

## See what FreeRide is doing

```bash
freeride watch
```

Tails live failover events from a running gateway. Every request, every
provider attempt, every rate-limit, every retry. Useful for seeing
failover happen in real time, debugging "is my agent actually using
FreeRide", or just demoing.

```
[14:23:01.412] req_a3f8e2c1  ▶ request model=openrouter/free stream
[14:23:01.421] req_a3f8e2c1  → openrouter[k0] openrouter/free
[14:23:01.833] req_a3f8e2c1  ← openrouter[k0] 412ms RATE_LIMIT ✗ (retry-after 47s)
[14:23:01.835] req_a3f8e2c1  → groq[k0] openrouter/free
[14:23:02.153] req_a3f8e2c1  ← groq[k0] 318ms OK ✓ first-chunk
[14:23:02.154] req_a3f8e2c1  ■ complete via groq
```

Events are written to `~/.freeride/events.jsonl`. Opt out with
`FREERIDE_EVENTS=0` if you don't want them. File caps at 1 MiB with
single-backup rotation.

## Commands

```
freeride serve                  start the gateway
freeride bind <agent>           write gateway URL into agent config
freeride watch                  tail live failover events
freeride bench                  per-provider latency comparison (needs serve running)
freeride telemetry [on|off]     manage telemetry
freeride list                   list available free models
freeride status                 show OpenClaw config + cache age (v2)
freeride auto                   auto-configure OpenClaw (v2)
freeride rotate                 swap primary if it fails (v2)
freeride-watcher                background daemon that rotates on failure
```

`freeride bench` example output:

```
$ freeride bench
Benchmarking 5 providers, 3 requests each via http://localhost:11343/v1...

provider              ok    p50      p95      tok/s
─────────────────────────────────────────────────────
groq                  3/3   142ms    287ms    98
cloudflare_wai        3/3   284ms    410ms    81
nvidia_nim            3/3   389ms    502ms    72
openrouter            3/3   412ms    721ms    63
huggingface           2/3   612ms    1840ms   41

Fastest: groq (142ms p50)
```

The v2 commands keep working for existing OpenClaw users.

## Providers

| Provider | Status | Notes |
|---|---|---|
| OpenRouter | shipped | full surface — chat, streaming, tools, vision, structured outputs |
| NVIDIA NIM | shipped | curated free-model allowlist; `NVIDIA_NIM_FREE_MODELS_OVERRIDE` to expand |
| Groq | shipped | hardcoded allowlist (Llama 3.x, Gemma 2, Mixtral, DeepSeek-R1-distill); `GROQ_FREE_MODELS_OVERRIDE` to expand |
| Cloudflare Workers AI | shipped | curated allowlist of cheap-per-neuron chat models; needs `CLOUDFLARE_ACCOUNT_ID` |
| HuggingFace Inference | shipped | full HF router catalog; budget governs access ($0.10/mo Free, $2/mo PRO) |
| Ollama (local) | shipped | local-only; mix with remote providers in the same failover chain. Set `OLLAMA_BASE_URL` to opt in. |

Adding a sixth: implement `freeride.core.provider.Provider` (`api_version=1`) in `freeride/providers/<name>.py`, register it in the conformance suite, done. See `CONTRIBUTING.md`.

## Agents

| Agent | `freeride bind` | Hot reload |
|---|---|---|
| OpenClaw | yes | needs restart |
| Aider | yes (`--scope home/cwd/git`) | needs restart |
| Continue | yes | yes |
| Hermes (NousResearch/hermes-agent) | yes | needs restart |

Or anything else: `OPENAI_API_BASE=http://localhost:11343/v1` + `OPENAI_API_KEY=any`.

## Claude Code skill

If you use Claude Code, install the FreeRide skill so Claude knows how to
detect, wire, and troubleshoot the gateway:

```
/plugin install https://github.com/Shaivpidadi/FreeRideV3
```

After install, Claude auto-invokes the skill when you mention FreeRide,
have it running on `localhost:11343`, or ask about routing across
free-tier providers. See [`skills/README.md`](skills/README.md) for
manual-install instructions.

## Docs

- [`docs/providers/SURVEY.md`](docs/providers/SURVEY.md) — Provider Protocol fit per provider (auth shape, free-tier semantics, error mapping)
- [`docs/providers/nvidia_nim.md`](docs/providers/nvidia_nim.md) — NVIDIA NIM specifics (free-model allowlist, 403=AUTH quirk)
- [`docs/agent-binders.md`](docs/agent-binders.md) — per-agent bind reference (config locations, hot-reload behavior, edge cases)
- [`docs/hermes.md`](docs/hermes.md) — Hermes identification + bind plan
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — adding a provider or binder

## License

MIT.
