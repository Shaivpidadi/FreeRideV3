# FreeRide

**The OpenAI-compatible gateway for every free-tier provider.**

<img width="800" height="468" alt="FreeRide failing over from an OpenRouter 429 to Groq in 42ms; the agent never knew" src="docs/assets/freeride-failover.gif" />

One local endpoint that fans out across **OpenRouter, Groq, NVIDIA NIM, HuggingFace, Cerebras, Cloudflare Workers AI**, and **your own Ollama**. Hit a rate limit, fail over to the next provider. Your agent never knows.

Also wraps **Claude Code**, **OpenAI Codex**, and **Google Gemini CLI** — run all three without paying any of their vendors.

> **102M+ tokens served in 35 days. $0 spent.**
> Routed through community free-tier keys via this gateway.
> Daily traffic: [free-ride.xyz/models](https://free-ride.xyz/models)

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
freeride run claude    # or: freeride run codex / freeride run gemini
```

That's it. No accounts, no subscriptions, no FreeRide cloud. Local-first, BYO keys, your machine talks to providers directly.

---

## Install

**macOS / Linux:**

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://api.free-ride.xyz/install.ps1 | iex"
```

The installer picks up `uv`, then `pipx`, then plain `pip` — whichever is on your system. Or [install from source](docs/install.md).

```bash
freeride init           # interactive — collects keys, writes ~/.freeride/.env
freeride serve          # gateway listens on localhost:11343
```

**Get keys (any one is enough; more = better failover):**

| Provider | Free tier | Get a key |
|---|---|---|
| OpenRouter | rotating free models | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Groq | daily token cap | [console.groq.com/keys](https://console.groq.com/keys) |
| NVIDIA NIM | credits per account | [build.nvidia.com](https://build.nvidia.com) |
| HuggingFace | $0.10/mo Free, $2/mo PRO | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| Cerebras | RPM / TPM caps | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| Cloudflare Workers AI | 10K neurons/day | [dash.cloudflare.com](https://dash.cloudflare.com) |
| Ollama (local) | no quota | install from [ollama.com](https://ollama.com) |

---

## Run a coding agent — free

Three of the major coding CLIs ship a `freeride run` wrapper that works with **no per-vendor key** and **no login**. The gateway translates between each CLI's native wire protocol and our routing layer; you get the polished agent UX of each CLI, paid for entirely by free-tier providers.

### Claude Code

```bash
freeride run claude
```

Inside the session, switch routing per request via `/model`:

| You type | What happens |
|---|---|
| `/model claude-opus-4-7` | Your Pro/Max subscription answers (passthrough to api.anthropic.com) — only if `claude login` has run |
| `/model freeride/free` | Free providers answer; smart-router picks the model |
| `/model freeride/fast` | Free; prefers Groq (low TTFT) |
| `/model freeride/quality` | Free; prefers OpenRouter (widest catalog) |
| `/model freeride/coding` | Free; pinned to a code-tuned model that reliably emits tool_use blocks |

Full guide: [`docs/agents/claude-code.md`](docs/agents/claude-code.md).

### OpenAI Codex

```bash
freeride run codex
```

Whatever model the CLI picks (`gpt-5-codex`, `gpt-5`, etc.) is routed to a free upstream provider. The gateway translates the Responses-API wire format (with full SSE event protocol — `response.output_item.added` → `output_text.delta` → `output_item.done` → `response.completed`) so the CLI parses everything natively.

Note: codex uses `bubblewrap` for shell-tool sandboxing; on systems without it, file/shell tool calls fail (the model still works). Full guide: [`docs/agents/codex.md`](docs/agents/codex.md).

### Google Gemini CLI

```bash
freeride run gemini
```

Any `gemini-*` model name routes to a free upstream provider. Translator handles Google's `{contents, tools, generationConfig}` shape both directions. Full guide: [`docs/agents/gemini.md`](docs/agents/gemini.md).

### Any other agent / SDK

```bash
# Aider / Continue.dev / hermes / your-own-tool — anything that speaks OpenAI:
freeride bind aider
freeride bind continue
# or just point it at the gateway directly:
OPENAI_API_BASE=http://localhost:11343/v1
OPENAI_API_KEY=any-string-here
```

---

## How failover works

Per-request the chain is **(provider, key)**, sorted by recent health:

1. Try the head pair.
2. **`RATE_LIMIT`** or **`AUTH`** error → mark the key as cooling, try the next key on the same provider.
3. **`MODEL_NOT_FOUND`** or **`QUOTA_EXHAUSTED`** → skip to the next provider.
4. **5xx / TIMEOUT** → next pair.
5. First successful response — stamp `X-FreeRide-Provider` + `X-FreeRide-Request-Id` headers and ship.

If every pair fails, you get a structured 503 with a per-provider breakdown so debugging is one log line, not five round-trips. Mid-stream errors after the first chunk shipped are logged but don't break the client (we can't un-ship bytes).

**Smart routing for `model: "auto"`:** the resolver scores every free model in the catalog by health × popularity (from the public [models leaderboard](https://free-ride.xyz/models)) and picks the best one. Run `freeride audit-models` once after install to cache health probes locally so the first real request isn't a cold start.

Deeper: [`docs/architecture/failover.md`](docs/architecture/failover.md).

---

## Providers

| Provider | Surface | Notes |
|---|---|---|
| OpenRouter | chat, streaming, tools, vision, structured outputs, embeddings | full surface — the most-used provider in our routing |
| NVIDIA NIM | chat + embeddings | curated free-model allowlist; `NVIDIA_NIM_FREE_MODELS_OVERRIDE` to expand |
| Groq | chat | Llama 3.x, Gemma 2, Mixtral, DeepSeek-R1-distill; daily token cap |
| Cloudflare Workers AI | chat | cheap-per-neuron models; needs `CLOUDFLARE_ACCOUNT_ID` |
| HuggingFace Inference | chat + embeddings | full HF router catalog; budget governs access |
| Cerebras | chat | fastest Llama / Qwen inference; no embeddings |
| Ollama (local) | chat | local-only; can mix with remote in the same failover chain |

Adding a new provider: implement `freeride.core.provider.Provider` in `freeride/providers/<name>.py`, register it in the conformance suite. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Multi-key rotation

Provide more than one key per provider with a numbered suffix:

```bash
OPENROUTER_API_KEY=sk-or-v1-aaa     # primary
OPENROUTER_API_KEY_2=sk-or-v1-bbb
OPENROUTER_API_KEY_3=sk-or-v1-ccc
```

The router tries them in health order. A 429 on one key cools it for the next 60s and rotates to the sibling key — no provider switch needed. On startup `freeride keys` shows which keys are available vs cooling.

---

## See what the gateway is doing

```bash
freeride doctor                # static checks: keys, ports, /etc/hosts, common gotchas
freeride doctor --claude-code  # the same + Claude-Code-specific probes
freeride audit-models          # probe every free model on every key; cache the results
freeride bench                 # measure p50/p95/tok-s per provider
```

Tail live events:

```bash
tail -f ~/.freeride/events.jsonl
```

Each line is a JSON event: routing decisions, provider attempts, response statuses, mid-stream errors. Same schema the marketing site reads to render the [live token counter](https://free-ride.xyz) and [provider leaderboard](https://free-ride.xyz/models).

---

## Telemetry

A small beacon ships hourly with **counts only**: tokens served, request count, active providers, uptime hours, OS, version, and a per-install UUID. **Never sent:** prompts, completions, model IDs, API keys, hostname, IP.

```bash
freeride telemetry        # audit what the next beacon would post
freeride telemetry off    # opt out
```

The aggregate is what powers [free-ride.xyz/models](https://free-ride.xyz/models). Default on; explicit disclosure banner prints on first run.

---

## Commands

```
freeride init           interactive setup wizard — prompts for keys, writes ~/.freeride/.env
freeride serve          start the gateway on :11343
freeride run <cli>      wrap a CLI (claude / codex / gemini / anything) — points it at the gateway
freeride bind <agent>   write the agent's config so it uses the gateway permanently
freeride doctor         pre-flight checks: keys, ports, hosts file, common gotchas
freeride keys           which provider keys are available vs cooling
freeride audit-models   probe every free model; cache health locally
freeride bench          measure p50/p95/tok-s per provider
freeride list           list available free models
freeride telemetry      manage the hourly aggregate beacon
```

---

## Docs

- **Agents**
  - [`docs/agents/claude-code.md`](docs/agents/claude-code.md) — Claude Code setup, `/model` modes, troubleshooting
  - [`docs/agents/codex.md`](docs/agents/codex.md) — OpenAI Codex setup, bwrap notes, model selection
  - [`docs/agents/gemini.md`](docs/agents/gemini.md) — Google Gemini CLI setup, auth flow, model selection
  - [`docs/agents/binders.md`](docs/agents/binders.md) — Aider, Continue, OpenClaw — per-agent `freeride bind` reference
  - [`docs/agents/hermes.md`](docs/agents/hermes.md) — NousResearch Hermes agent integration
- **Providers**
  - [`docs/providers/SURVEY.md`](docs/providers/SURVEY.md) — per-provider fit (auth, free-tier semantics, error mapping)
  - [`docs/providers/nvidia_nim.md`](docs/providers/nvidia_nim.md) — NVIDIA NIM specifics
- **Architecture**
  - [`docs/architecture/failover.md`](docs/architecture/failover.md) — failover chain, cooldown, health tracking
  - [`docs/architecture/translators.md`](docs/architecture/translators.md) — how the Anthropic / Google / OpenAI-Responses translators work
- **Other**
  - [`CONTRIBUTING.md`](CONTRIBUTING.md) — adding a provider, a CLI wrapper, or a binder
  - [`SECURITY.md`](SECURITY.md) — reporting vulnerabilities

---

## License

MIT.
