# FreeRide

**Free AI inference for real work — as a coding agent, or as a gateway for your own tools.**

FreeRide routes requests across free-tier providers — **OpenRouter, Google Gemini, Ollama Cloud, Z.ai, Groq, NVIDIA NIM, Cloudflare Workers AI, xKiro**, a dozen smaller gateways, and **your own Ollama** — with automatic failover when one rate-limits or errors. No vendor subscription, no cloud middleman: your machine talks to the providers directly with your own free keys. There are two ways in; both run on the same engine.

> **6B+ tokens served since February 2026. $0 spent.**
> Routed through community free-tier keys via this gateway, across 550+ installs.
> Live counter and per-model traffic: [free-ride.xyz](https://free-ride.xyz) · [free-ride.xyz/models](https://free-ride.xyz/models)

<img width="800" height="468" alt="FreeRide failing over from an OpenRouter 429 to Groq in 42ms; the agent never knew" src="docs/assets/freeride-failover.gif" />

---

## Pick your path

### 🤖 "I want a coding agent" → ridex

A fast, native agent (our fork of [vercel-labs/fx](https://github.com/vercel-labs/fx), Apache-2.0) that reads files, edits code, and runs commands — every model call served free through FreeRide. One command installs the agent and the gateway, supervises the daemon, and walks you through keys on first run:

```bash
curl -sSL https://api.free-ride.xyz/ridex.sh | sh
ridex                                          # interactive agent
ridex ask "reply with the single word pong"    # or one-shot
```

Agent releases: [github.com/Shaivpidadi/ridex](https://github.com/Shaivpidadi/ridex). macOS + Linux (arm64/x86_64).

### 🔌 "I have my own tools" → the gateway

A local, OpenAI-compatible endpoint on `localhost:11343` that any agent, SDK, or script can point at — Aider, Continue.dev, LangChain, raw `openai` clients, your own code:

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
freeride init && freeride serve
```

```bash
OPENAI_API_BASE=http://localhost:11343/v1
OPENAI_API_KEY=any-string-here   # inbound auth is ignored; your real keys stay server-side
```

It also natively serves the **Anthropic** (`/v1/messages`), **OpenAI Responses** (`/v1/responses`), **Gemini** (`:generateContent`), and **fx gateway** wire protocols, plus `/v1/embeddings` — so most tools work unmodified. Prefer the big-vendor CLIs' UX? `freeride run claude|codex|gemini` wraps them with no vendor login. macOS + Linux + Windows.

Already running ridex? You have the gateway too — one shared daemon on `:11343` serves your agent sessions and your own tools with the same keys, failover chain, and cooldowns.

---

## Keys

Any one is enough; more = better failover. Collected by `freeride init` (or ridex's first run) and stored **only** on your machine in `~/.freeride/.env`:

| Provider | Free tier (checked 2026-10-16) | Get a key |
|---|---|---|
| OpenRouter | rotating `:free` models; 20 RPM, 50 req/day account-wide (1,000/day once you've ever bought $10 of credits) | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Google Gemini | Flash / Flash-Lite / Gemma free of charge; limits shown in AI Studio. Create the key in a project **without** billing: a prepaid-billing project answers 402 instead of serving the free tier | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Ollama Cloud | Free plan: gpt-oss 20b/120b, Nemotron 3 nano/super/ultra, gemma4:31b, beam; 1 concurrent request | [ollama.com/settings/keys](https://ollama.com/settings/keys) |
| Z.ai (GLM) | GLM-4.7-Flash, GLM-4.5-Flash, GLM-4.6V-Flash free | [z.ai](https://z.ai/manage-apikey/apikey-list) |
| xKiro | 500K tokens/day on 50+ free-tier models (Qwen3 Coder, Ministral, DeepSeek, MiniMax) | [xkiro.com](https://xkiro.com) |
| Mistral | Free mode where the console offers it; some accounts see paid plans only | [console.mistral.ai](https://console.mistral.ai/api-keys) |
| SambaNova | docs list a free tier, but a fresh account needs a payment method on file (402 otherwise) | [cloud.sambanova.ai](https://cloud.sambanova.ai/apis) |
| Groq | per-model RPM / daily token caps (see your console's limits page) | [console.groq.com/keys](https://console.groq.com/keys) |
| NVIDIA NIM | recurring per-account rate limit (evaluation terms) | [build.nvidia.com](https://build.nvidia.com) |
| Cloudflare Workers AI | 10K neurons/day | [dash.cloudflare.com](https://dash.cloudflare.com) |
| HuggingFace | **PRO only** ($2/mo credit); free accounts get no monthly credit | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| Cerebras | **trial only**: one-time $5, card required, expires after 30 days | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| Ollama (local) | no quota | install from [ollama.com](https://ollama.com) |

No key at all? `FREERIDE_KEYLESS=ovh,blockrun` in `~/.freeride/.env` turns on anonymous
gateways (OVHcloud AI Endpoints at 2 req/min per model, BlockRun at 30 req/min). They
are shared per IP and slow, so they're opt-in; `kilo` is also available but states it
may train on prompts. Full landscape and what's coming next:
[`docs/providers/EXPANSION-RESEARCH.md`](docs/providers/EXPANSION-RESEARCH.md).

## Why this doesn't fall over

Free tiers are flaky — that's the whole reason FreeRide exists. Whichever path you picked, the same engine keeps a single provider's bad minute from ever reaching you:

- **The gateway is a supervised daemon** (via the ridex installer): registered with launchd (macOS) or a systemd user unit (Linux), a crash restarts it in seconds. `ridex start|stop|restart|doctor` manage it, and `ridex stop` sticks until you say otherwise. (Standalone `freeride serve` users manage the process themselves, as before.)
- **Every request carries a fallback ladder.** If the serving provider rate-limits, runs out of free inference, or retires the model mid-session, the gateway silently retries on the next provider's best tool-capable model — inside the same response. Failed candidates are remembered for a few minutes so consecutive turns don't re-pay the cost.
- **The agent can diagnose its own plumbing.** ridex ships with a `freeride` skill: when requests fail it runs the (pre-approved, read-only) diagnostics — `freeride doctor`, `freeride keys`, the health probe — reads the structured error taxonomy, and tells you the exact fix.
- **Tool calls are non-negotiable.** The default `freeride/coding` route pins to models proven to emit correct tool calls; providers whose catalogs can't do tools are never handed agent traffic.

Inside a session, `/model` switches routing per request: `freeride/coding` (default), `freeride/fast` (Groq-first, low TTFT), `freeride/quality` (OpenRouter-first, widest catalog), `freeride/free` / `auto` (pure smart-routing), or any concrete model id from `ridex models`.

---

## Wrap the big-vendor CLIs

Prefer Claude Code, OpenAI Codex, or Gemini CLI's UX? `freeride run` points them at the gateway — no per-vendor key, no login:

```bash
freeride run claude    # /model freeride/coding etc. inside the session
freeride run codex     # Responses-API wire format translated natively
freeride run gemini    # Google's {contents, tools, generationConfig} shape both ways
```

Guides: [`docs/agents/claude-code.md`](docs/agents/claude-code.md) · [`docs/agents/codex.md`](docs/agents/codex.md) · [`docs/agents/gemini.md`](docs/agents/gemini.md). For Aider, Continue.dev, and friends: `freeride bind <agent>` ([`docs/agents/binders.md`](docs/agents/binders.md)).

---

## How failover works

Every request walks a chain of **(provider, key)** pairs sorted by recent health, and each provider carries its own short list of candidate models:

1. **`auto` and the `freeride/*` presets** resolve to the best model on *each* configured provider (ranked by health × popularity), not one model carried across the chain. The first provider is asked for its best model; if it fails, the next provider is asked for *its* best model.
2. **Key-level failures** (`AUTH`, a key-scoped `RATE_LIMIT`) cool that key and rotate to the next key on the same provider.
3. **Model-level failures** (`MODEL_NOT_FOUND`, a tier-gated model, a model-scoped `RATE_LIMIT`, `TIMEOUT`) move to the provider's next candidate model with the same key, and demote the failed pair for a few minutes so the next request skips it.
4. **`QUOTA_EXHAUSTED`** (out of credits, payment required) benches the key and leaves the provider.
5. First successful response — stamp `X-FreeRide-Provider` + `X-FreeRide-Request-Id` headers and ship.

All of that happens before the first byte reaches the client, so a stream that fails over still looks like one response. An upstream that dies mid-stream after output has shipped ends the turn as an explicit error (agents retry it) rather than a silently truncated answer. If every pair fails, you get a structured 503 with a per-provider breakdown.

Explicit model ids are sent in each provider's own spelling (`nvidia/nemotron-3.5-lightning:free` on one gateway, `nvidia/nemotron-3.5-lightning` on another) and otherwise behave as you'd expect: a dead model named explicitly fails rather than being swapped.

**Presets:** `freeride/coding` (tool-capable models first), `freeride/fast` (Groq / SambaNova first), `freeride/quality` (OpenRouter / Gemini / Mistral first), `freeride/free` or `auto` (pure smart routing). They work on every route, including plain `/v1/chat/completions`. Run `freeride audit-models` once after install to cache live health probes so the first real request isn't a cold start.

Deeper: [`docs/architecture/failover.md`](docs/architecture/failover.md).

---

## Providers

Built-in plugins with their own wire handling:

| Provider | Surface | Notes |
|---|---|---|
| OpenRouter | chat, streaming, tools, vision, structured outputs, embeddings | the most-used provider in our routing; `:free` models, 50 req/day account-wide |
| Google Gemini | chat, streaming, tools | OpenAI-compatible endpoint; Gemini 3 thought signatures are cached and replayed so multi-turn tool use survives the round trip |
| NVIDIA NIM | chat, embeddings | curated free-model allowlist; `NVIDIA_NIM_FREE_MODELS_OVERRIDE` to expand |
| Groq | chat, streaming, tools | gpt-oss 20b/120b, Qwen, Llama; per-model RPM and daily token caps |
| Cloudflare Workers AI | chat, streaming, tools | 10K neurons/day; needs `CLOUDFLARE_ACCOUNT_ID` |
| HuggingFace Inference | chat, embeddings | full router catalog; PRO credit governs access |
| Cerebras | chat, streaming, tools | fastest inference around; trial credit only since 2026 |
| Ollama (local) | chat, embeddings | your own models; mixes with remote providers in the same chain |

Spec-driven providers (one row each in `freeride/core/provider_specs.py`, served by the generic OpenAI-compatible adapter):

| Provider | Enable with | Notes |
|---|---|---|
| Ollama Cloud | `OLLAMA_CLOUD_API_KEY` | Free-plan roster pinned (gpt-oss, Nemotron 3, gemma4, beam); gated models answer 402 and are skipped, not benched |
| xKiro | `XKIRO_API_KEY` | 50+ free-tier models with tool calling; 500K tokens/day |
| Z.ai (GLM Flash) | `ZAI_API_KEY` | hidden-reasoning models, so a 90 s first-byte budget |
| Mistral | `MISTRAL_API_KEY` | where the console offers Free mode; chat + embeddings |
| SambaNova | `SAMBANOVA_API_KEY` | per-model daily caps, so a 429 benches the model, not the key; needs a payment method on file |
| OVHcloud · Kilo · BlockRun | `FREERIDE_KEYLESS=ovh,kilo,blockrun` | anonymous, no account; shared per IP; Kilo may train on prompts |
| Requesty · Routeway · Septor · ElectronHub · Lucidity · DreamPrompting · Waterfall · LLM7 · LongCat | `<NAME>_API_KEY` | the no-card long tail; same adapter, same failover |

Every spec row gets the same treatment: catalog discovery with a chat-shape filter, retry hints read from headers *and* error bodies, tier-gated 402/403s treated as model problems rather than key problems, and detection of gateways that quietly answer with a different model than the one asked for.

Adding an OpenAI-compatible provider is one spec row (base URL, env var, free-model rule, quirks); the conformance suite runs every row. Anything with its own wire format implements `freeride.core.provider.Provider` in `freeride/providers/<name>.py`. See [`docs/providers/openai_compat.md`](docs/providers/openai_compat.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md).

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
ridex doctor                   # agent binary + daemon + key status in one report
freeride doctor                # static checks: keys, ports, /etc/hosts, common gotchas
freeride audit-models          # probe every free model on every key; cache the results
freeride bench                 # measure p50/p95/tok-s per provider
```

Tail live events:

```bash
tail -f ~/.freeride/events.jsonl
```

Each line is a JSON event: routing decisions, provider attempts, ladder fallbacks, response statuses, mid-stream errors. Same schema the marketing site reads to render the [live token counter](https://free-ride.xyz) and [provider leaderboard](https://free-ride.xyz/models).

---

## Telemetry

A small beacon ships hourly with **counts only**: tokens served (input and output), request count, the same counts split per provider name, active providers, uptime hours, OS, version, and a per-install UUID. **Never sent:** prompts, completions, model IDs, API keys, hostname, IP.

```bash
freeride telemetry        # audit what the next beacon would post
freeride telemetry off    # opt out
```

The aggregate is what powers [free-ride.xyz/models](https://free-ride.xyz/models). Default on; explicit disclosure banner prints on first run.

---

## Commands

```
ridex                   interactive coding agent (auto-starts the gateway daemon)
ridex ask <prompt>      one noninteractive agent request
ridex models            list available models
ridex start|stop|restart  manage the gateway daemon (stop sticks)
ridex doctor            agent + daemon + key health report

freeride init           interactive setup wizard — prompts for keys, writes ~/.freeride/.env
freeride serve          start the gateway on :11343 (the daemon runs this for you)
freeride run <cli>      wrap a CLI (claude / codex / gemini) — points it at the gateway
freeride bind <agent>   write the agent's config (openclaw, aider, continue, hermes, opencode)
freeride doctor         pre-flight checks: keys, ports, hosts file, common gotchas
freeride keys           which provider keys are available vs cooling
freeride reload         hot-reload provider keys on a running gateway
freeride audit-models   probe every free model; cache health locally
freeride bench          measure p50/p95/tok-s per provider
freeride list           list every configured provider's free models
freeride telemetry      manage the hourly aggregate beacon
```

---

## Docs

- **The agent**
  - [github.com/Shaivpidadi/ridex](https://github.com/Shaivpidadi/ridex) — the ridex agent (fork of vercel-labs/fx)
  - [`internal-docs/RIDEX_PLAN.md`](internal-docs/RIDEX_PLAN.md) — architecture decisions + verification log
- **Wrapped CLIs**
  - [`docs/agents/claude-code.md`](docs/agents/claude-code.md) — Claude Code setup, `/model` modes, troubleshooting
  - [`docs/agents/codex.md`](docs/agents/codex.md) — OpenAI Codex setup, bwrap notes, model selection
  - [`docs/agents/gemini.md`](docs/agents/gemini.md) — Google Gemini CLI setup, auth flow, model selection
  - [`docs/agents/binders.md`](docs/agents/binders.md) — Aider, Continue, OpenClaw, OpenCode — per-agent `freeride bind` reference
  - [`docs/agents/hermes.md`](docs/agents/hermes.md) — NousResearch Hermes agent integration
- **Providers**
  - [`docs/providers/openai_compat.md`](docs/providers/openai_compat.md) — the spec-driven adapter: free-model rules, quota scopes, retry hints, keyless mode, Gemini specifics
  - [`docs/providers/SURVEY.md`](docs/providers/SURVEY.md) — per-provider fit (auth, free-tier semantics, error mapping)
  - [`docs/providers/EXPANSION-RESEARCH.md`](docs/providers/EXPANSION-RESEARCH.md) — free-tier landscape (Oct 2026), which providers to add next, and what the plugin layer needs first
  - [`docs/providers/nvidia_nim.md`](docs/providers/nvidia_nim.md) — NVIDIA NIM specifics
- **Architecture**
  - [`docs/architecture/failover.md`](docs/architecture/failover.md) — failover chain, cooldown, health tracking
  - [`docs/architecture/translators.md`](docs/architecture/translators.md) — how the Anthropic / Google / OpenAI-Responses / fx translators work
- **Other**
  - [`CONTRIBUTING.md`](CONTRIBUTING.md) — adding a provider, a CLI wrapper, or a binder
  - [`SECURITY.md`](SECURITY.md) — reporting vulnerabilities

---

## License

MIT. The ridex agent is a fork of [vercel-labs/fx](https://github.com/vercel-labs/fx) (Apache-2.0); its license and notices ship with every release tarball.
