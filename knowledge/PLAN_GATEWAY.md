# FreeRide v3 — Gateway Architecture (Revised Plan)

> **Status:** Draft. Supersedes the gateway-relevant sections of `PLAN.md`.
> Living document. Read alongside `PLAN.md`; differences are explicit in §3.
>
> **Where development happens:** Branch `v3` on the existing `Shaivpidadi/FreeRide` repo, local-only, until Phase 3 lands. Migration to a separate private repo deferred.

---

## 1. One-sentence pitch

**FreeRide is a local OpenAI-compatible server that keeps your inference free, indefinitely, by orchestrating across every free-tier provider and every key you have, transparently to whatever agent or SDK is calling it.**

## 2. Why this is different from `PLAN.md`

The original V3 plan is a **config-writer**: it generalizes v2 by abstracting providers AND consumers, and writes config files for each agent runtime. The user-facing promise was "OpenClaw works on free AI today; eventually Hermes, Aider, Continue, OpenCode each get a Consumer plugin."

This plan is a **gateway**: FreeRide *is* the route. It exposes one OpenAI-compatible HTTP endpoint. Any agent that can change its base URL points at FreeRide and gets free AI. The Consumer plugin abstraction collapses into a tiny `freeride bind <agent>` helper that writes one URL into one file — no per-agent abstraction layer.

The user's stated goal — *"all the logic of connecting, doing orchestration, attaching multiple providers should be on FreeRide so regardless of the platform/agent/model user can use it"* — is what a gateway delivers. A config-writer can't, because the long tail of agent config formats will always exceed plugin-writing capacity.

## 3. Concrete diffs vs `PLAN.md`

| `PLAN.md` says | This plan says | Why |
|---|---|---|
| §10: "Becoming a routing proxy" is out of scope | **Flipped** — the routing proxy *is* the product | Only architecture that delivers "any platform/agent/model" |
| §6.1: `consumers/` package with `Consumer` Protocol | No `Consumer` abstraction; `freeride bind` writes one URL | 20× reduction in surface area |
| §6.2: `Consumer.format_model_ref` / `parse_model_ref` | Gateway owns the model namespace; no consumer prefix logic | OpenClaw's `openrouter/` prefix is a v2 implementation leak |
| §7: validation = "2 providers × 2 consumers" | Validation = "2 providers × 2 real clients survive a mid-request 429 invisibly" | The thing we actually need to prove is mid-flight failover |
| §11 Phase 1: refactor v2 logic into `core/` + provider + consumer | Phase 1: same refactor minus consumers; plus `requests` → `httpx` port | Async server requires async HTTP client end-to-end |
| §9 open question 3 (versioning): deferred | **Decided**: `Provider.api_version = 1` from day one | Cheap now, breaking change later |
| §9 open question 4 (migration): deferred | **Decided**: `freeride auto` CLI surface frozen; everything else can change | One v2 command users care about |

## 4. Architecture

```
                ┌──────────────────────────────────────────┐
                │                FreeRide                  │
[Any agent] ───►│  POST /v1/chat/completions               │───► OpenRouter
[curl]      ───►│  POST /v1/embeddings  (later)            │───► NVIDIA NIM
[SDK]       ───►│  GET  /v1/models     (returns "free now")│───► Groq
                │                                          │───► Cloudflare WAI
                │  ┌──────────────────────────────────┐    │───► Hugging Face
                │  │ Resolver (model + provider +     │    │
                │  │           key, by health/quota)  │    │
                │  ├──────────────────────────────────┤    │
                │  │ Provider registry (plugins)      │    │
                │  ├──────────────────────────────────┤    │
                │  │ Health tracker (latency, 429s,   │    │
                │  │   per-tuple quota state)         │    │
                │  ├──────────────────────────────────┤    │
                │  │ Key pool (per-provider cooldowns)│    │
                │  ├──────────────────────────────────┤    │
                │  │ Probe loop (lifted v2 watcher,   │    │
                │  │   provider-agnostic)             │    │
                │  └──────────────────────────────────┘    │
                └──────────────────────────────────────────┘
```

Wire protocol: **OpenAI-compatible chat completions only** for v3.0. That's the lingua franca every agent already speaks. Anthropic Messages API is a Phase 6+ decision.

## 5. What carries forward from v2 (still load-bearing)

These come straight from the v2 codebase and remain non-negotiable:

1. **Free-detection per provider** — OpenRouter's dual signal (`pricing.prompt == 0` ∨ `:free` suffix) lifted into `providers/openrouter.py`; per-provider rules elsewhere.
2. **Live probe with `max_tokens: 5`** — `_test_model` pattern, now used by background probes and cold-start ranking, not for config writes.
3. **Per-process key cooldown** (`_RATE_LIMITED_KEYS`, 120s) — the in-process structure is exactly right; persist across gateway restarts.
4. **Attribution headers** — `HTTP-Referer` + `X-Title` for OpenRouter, generalized to per-provider attribution (default empty for providers without it).
5. **Atomic state writes** — lift `watcher._atomic_write` into `core/state.py`; use everywhere.
6. **Free-only, ever** — project memory rule, no paid fallbacks.
7. **No auto-installed services** — gateway is foreground; document `nohup` / `launchd` / `tmux` patterns. No shipped `systemd` units.
8. **Recovery lives outside the inference loop** — gateway IS outside the agent, so 429-recovery is an inherent property.
9. **Verify against the real consumer, not inferred behavior** — for the gateway, this means: integration tests against a real agent (OpenClaw, Aider) calling the gateway end-to-end, not just unit tests on request shapes.

## 6. What gets dropped from the old plan

- **`Consumer` Protocol and `consumers/` package.** Replaced by `freeride bind <agent>` — a two-line helper per agent that writes one URL into one file. Aider gets `OPENAI_API_BASE`. Continue gets one block in `~/.continue/config.json`. OpenClaw gets a base-URL set in its auth profile. No abstraction.
- **OpenClaw routing-prefix logic** (`format_model_for_openclaw` / `_config_primary_to_api_id`). The gateway controls its own model namespace; agents see what we expose, not what OpenRouter exposes.
- **`Consumer.write(primary, fallbacks)` shape and the "preserve unrelated keys" tested invariant.** Gone. The gateway doesn't write consumer configs; `freeride bind` only sets a base URL.
- **The 2 providers × 2 consumers validation matrix.** Replaced — see §10.

## 7. Provider plugin contract

```python
class Provider(Protocol):
    name: str
    api_version: int = 1                    # frozen from day one

    # --- discovery & probing ---
    def list_free_models(self, key: str) -> list[Model]: ...
    def probe(self, model_id: str, key: str) -> ProbeResult: ...

    # --- request forwarding (the new core capability) ---
    async def forward_chat(
        self, request: ChatRequest, model_id: str, key: str,
    ) -> ChatResponse: ...
    async def forward_chat_stream(
        self, request: ChatRequest, model_id: str, key: str,
    ) -> AsyncIterator[ChatStreamEvent]: ...

    # --- error classification (replaces v2's ad-hoc strings) ---
    def classify_error(self, exc_or_response) -> ErrorKind: ...
    def retry_after_hint(self, response) -> Optional[int]: ...

    # --- request stamping ---
    def auth_header(self, key: str) -> dict[str, str]: ...
    def attribution_headers(self) -> dict[str, str]: ...    # default: {}


class ErrorKind(Enum):
    OK = "ok"
    RATE_LIMIT = "rate_limit"            # transient, try another key
    QUOTA_EXHAUSTED = "quota_exhausted"  # this key is dead until tomorrow
    MODEL_NOT_FOUND = "model_not_found"  # try another model
    UNAVAILABLE = "unavailable"          # provider 5xx, transient
    TIMEOUT = "timeout"
    AUTH = "auth"                        # key invalid
    UNKNOWN = "unknown"
```

`ChatRequest` / `ChatResponse` mirror OpenAI's schema. Tool calls, vision, structured outputs, `response_format`, `logprobs` pass through opaquely; provider plugins adapt where their API differs.

Plugin discovery via Python entrypoints (unchanged from PLAN.md §6.5):

```toml
[project.entry-points."freeride.providers"]
nvidia_nim = "freeride_provider_nim:NIMProvider"
```

## 8. The hard engineering problems

These are not in `PLAN.md` and will eat real time. Calling them out so they don't surprise us in Phase 2.

1. **Mid-stream failover.** If bytes have already streamed to the client, we cannot transparently retry. Decision: **buffer the first chunk**; retry if zero bytes have shipped (catches the common case — 429s usually arrive on initial response). Document mid-stream failures as a known limitation. Always-buffer-to-completion is rejected — it kills chat UX.

2. **Tool calls / structured outputs / vision.** Pass-through, but error classification needs granularity. "Provider doesn't support tools" ≠ "model doesn't support tools" ≠ "request was malformed."

3. **Cancellation.** Client disconnects mid-request → propagate cancel upstream. Otherwise we burn quota on dropped requests. Standard async pattern, easy to miss.

4. **Per-(provider × model × key) health tracking.** Multiplicative state space. In-memory dict, bounded LRU; persist coarse summary on shutdown.

5. **Probe budget.** Background probes shouldn't burn quota. Cap top-N models per provider; longer interval (15 min); piggyback on real traffic — every successful real request updates health, so probes only fire when a tuple has been silent too long.

6. **Concurrency model.** Async end-to-end. **`requests` → `httpx`** is the single biggest mechanical lift in Phase 1. Server: FastAPI (or Starlette) on uvicorn.

7. **Logs default off.** The gateway sees every prompt. Default no-log; `--verbose` opt-in only. Privacy-positive.

8. **Port management.** Default `localhost:11343`. `--port` override. Clear error if port is in use; don't pick a different port silently (agents have it hardcoded).

## 9. Refined roadmap

### Phase 0 — Decisions (this week, no code)
Update this document and `PLAN.md` with these committed calls:
- **D1:** V3 is a gateway, not a config-writer. PLAN.md §10 non-goal *flipped*.
- **D2:** Wire protocol = OpenAI-compatible chat completions. Anthropic Messages = deferred to Phase 6+.
- **D3:** HTTP server = FastAPI on uvicorn. HTTP client = `httpx` (async).
- **D4:** Streaming failover = buffer-first-chunk; mid-stream failures surface to client.
- **D5:** `Provider.api_version = 1` from day one.
- **D6:** No `Consumer` abstraction; `freeride bind <agent>` helper per supported agent.
- **D7:** Local-first. No hosted gateway. No auto-install services.
- **D8:** Logs off by default; `--verbose` opt-in only.
- **D9:** `freeride auto` CLI surface frozen; everything else can change.
- **D10:** Naming — keep `FreeRide`. It generalizes (still riding free).

### Phase 1 — Lift v2 logic into a library (1–2 weeks)
Pure refactor; no behavior change.
- Create `freeride/{core,providers,server,cli}/` package layout.
- Port v2 logic into `providers/openrouter.py` against the v0 Provider interface.
- Port `requests` calls to `httpx` (sync + async surfaces both available).
- Lift atomic-write into `core/state.py`; use everywhere (fixes a latent v2 bug — `save_openclaw_config` is currently non-atomic).
- Persist key cooldown to disk.
- v2 CLI commands (`auto`, `list`, `status`, `rotate`, watcher) keep working but become thin wrappers around the library.
- **Acceptance:** `freeride auto` end-to-end behavior identical to v2; v2 tests pass unchanged.

### Phase 2 — Minimal gateway (2–3 weeks)
- `freeride serve` starts FastAPI on `localhost:11343`.
- `POST /v1/chat/completions` (non-streaming first), single provider (OpenRouter), multi-key, mid-request retry on 429 / auth-failure.
- `GET /v1/models` returns currently-known free chat models, ranked.
- Health tracker, key pool, resolver — all in-process.
- **Acceptance:** `OPENAI_API_BASE=http://localhost:11343/v1 OPENAI_API_KEY=any aider` works against a real Aider install. No code in Aider.

### Phase 3 — Streaming + second provider (2–3 weeks)
- Streaming with buffer-first-chunk failover.
- Add `providers/nvidia_nim.py`. NIM forces real heterogeneity: free-credit semantics, no `:free` suffix, different probe convention.
- Background probe loop (lifted v2 `watcher.py` logic), now provider-agnostic.
- **Acceptance:** `freeride serve` running with both OpenRouter and NIM keys configured. Hit OpenRouter's free-tier limit mid-conversation; gateway transparently fails over to NIM; agent never sees the 429. **Measured: zero `core/` changes when adding NIM.** If `core/` had to change, the seam is wrong — regroup.

### Phase 4 — Feature passthrough (2–3 weeks)
- Tool calls, structured outputs (`response_format: json_schema`), vision, `logprobs`. Passthrough; classify provider quirks per-plugin.
- `freeride bind <openclaw|aider|continue>` helpers (one per agent, ad-hoc, no abstraction).
- **Acceptance:** Continue user with default config + `freeride bind continue` → free AI with cross-provider failover, no manual tweaks.

### Phase 5 — Polish + ship publicly
- Public release; one-page docs; install script; brand stays "free AI for everyone."
- **Decision point** for old PLAN.md §8.3: replace v2 entirely (recommended — v2 was a stepping stone, the gateway IS the product) vs keep two products (don't — split focus kills small projects).
- Spin up a separate repo if Phase 5 reveals the gateway has reach beyond OpenClaw users.

### Phase 6 — Optional later
- Anthropic Messages API surface (for Claude-shaped clients).
- Embeddings endpoint (`/v1/embeddings`).
- Self-hosted multi-tenant mode (probably never; local-first is the value prop).

## 10. Validation criterion (replaces PLAN.md §7)

The 2 × 2 matrix from `PLAN.md` was about whether the Provider/Consumer abstractions held. With consumers gone, the matrix becomes:

> **2 providers × 2 real clients, surviving a mid-request 429 invisibly to the client, with zero `core/` code changes between adding provider A and provider B.**

Concrete:
- Provider A = OpenRouter, Provider B = NVIDIA NIM.
- Client A = OpenClaw via `freeride bind openclaw`. Client B = raw `curl` script.
- Test: artificially exhaust OpenRouter free-tier mid-request (or rate-limit a key) → gateway fails over to NIM → client receives a clean response. No errors visible to client.
- Measured: `git diff` between "before adding NIM" and "after adding NIM" shows changes only in `providers/nvidia_nim.py` and a registration line in `providers/__init__.py`. **If `core/` changed, the seam is in the wrong place.**

## 11. Trade-offs vs original PLAN.md

| Dimension | Old V3 (config-writer) | New V3 (gateway) |
|---|---|---|
| Engineering surface | N consumer plugins × M provider plugins | 1 server + M provider plugins |
| Reach (agents) | Only agents with a written plugin | Any agent supporting `OPENAI_API_BASE` |
| Failover latency | 60s watcher tick | Mid-request, single-digit ms |
| In request path | No | Yes (must be running, adds latency) |
| Privacy | Doesn't see prompts | Sees every prompt (default no-log) |
| Operational complexity | A CLI that occasionally writes a file | A long-running async HTTP server |
| Streaming concerns | None | Real (mid-stream failover) |
| Competitive positioning | Niche, OpenClaw-flavored | LiteLLM-with-curated-free-tiers |

The new V3 is **a bigger, more ambitious project** — but it actually delivers "free AI for all, regardless of platform/agent/model." The old V3 didn't, no matter how cleanly the abstractions came out, because the long tail of agents would always exceed plugin-writing capacity.

## 12. Competitive position

Incumbents in adjacent space: **LiteLLM** (open-source proxy, doesn't curate free tiers), **OpenRouter** (paid-first with a free tier on one provider — itself), **Cloudflare AI Gateway** and **Vercel AI Gateway** (paid, hosted).

What's uniquely FreeRide:
1. **Free-only by religion.** Every other gateway is built around billing infrastructure; "free" is a side feature. Here it's the entire point. That shapes a different product (curated free models, multi-provider rotation, no tier upsell paths).
2. **BYO keys, no margin.** Not in the inference business; routing for the user's own keys.
3. **Local-first.** Privacy-positive default; doesn't see prompts unless `--verbose` is passed.
4. **Cross-provider rotation as a first-class feature**, not an enterprise upsell.

Narrow but credible position. Don't drift from it.

## 13. Open questions (decide before Phase 2)

Most of `PLAN.md` §9 is resolved by the decisions in §9.D1–D10 above. What remains:

1. **Multi-provider model identity.** DeepSeek-V3 lives on OpenRouter, NIM, DeepInfra under different `api_id`s. Should `/v1/models` expose them as one logical entry (and the resolver picks the provider) or N entries (and the user picks)? **Tentative: one logical entry per "canonical" model**, with the resolver dispatching across providers. This is the gateway's killer feature — don't punt it to the user.

2. **Telemetry.** Now that the gateway sees real traffic, should it record (locally, opt-in) per-provider success / 429 / latency stats so users can see "your free AI saved you N requests this week"? Useful UX; not a privacy concern if local-only and aggregate.

3. **Quota visibility.** From accumulated request history per key, the gateway can compute "you have ~120 requests left today on this key." Surface where? `freeride status` extension; not a separate command.

4. **Anthropic Messages API surface.** Phase 6 question. Decision criterion: if ≥ 2 real Anthropic-API-shaped clients show up wanting it, do it. Otherwise hold.

## 14. Out of scope (explicit non-goals — supersedes PLAN.md §10)

- **Paid models, ever.**
- **Auto-installing services.** No `launchd` plists, `systemd` units, cron entries, shell-profile edits. Documented `nohup` / `launchd` patterns only.
- **Hosted/multi-tenant gateway.** Local-first is the value prop and the privacy story. No SaaS version.
- **Image-gen, audio-gen, embeddings (initially).** Chat-shaped models only for v3.0. Embeddings = Phase 6 maybe.
- **Rate-limit prediction / quota arbitrage.** We probe and react. We don't model provider rate limits in advance.
- **Local-only consumers (llama.cpp, LM Studio).** They don't need a gateway — they have local inference. Not the user we're serving.

## 15. Glossary

- **Provider** — a source of free AI models (OpenRouter, NVIDIA NIM, …).
- **Client** — anything that sends requests to the gateway (an agent like OpenClaw, an SDK, a `curl` script). Replaces "Consumer" from the old plan.
- **Resolver** — picks the `(provider, model, key)` tuple for an incoming request based on health and policy.
- **Tuple health** — per-(provider × model × key) success rate / latency / cooldown state.
- **Live probe** — real `chat/completions` request with `max_tokens: 5` to verify availability.
- **Failover** — switching to a different `(provider, model, key)` on error, transparently to the client.

## 16. References

### v2 carry-forward principles (project memory)
- `project_free_only.md` — free-models-only, no paid models.
- `project_no_auto_service_install.md` — no OS-level service auto-install.
- `feedback_verify_payload_before_committing.md` — verify against real consumer, not inferred behavior. For the gateway, this means real-client integration tests, not just request-shape unit tests.
- `feedback_no_claude_coauthor.md` — commit hygiene.

### v2 issues that motivated v3 (still relevant)
- **#11** — Hermes agent support. **Resolution under this plan:** if Hermes speaks `OPENAI_API_BASE`, no work needed beyond `freeride bind hermes`. If it doesn't, we don't build a Consumer plugin — we document how Hermes users point at the gateway, or close the issue as "use a different agent."
- **#12** — Fallbacks routing wrong provider. **Resolution under this plan:** N/A. The gateway controls model namespace and routing; no consumer-side fallback chain to mis-format.
- **#14** — NVIDIA NIM support. **Resolution under this plan:** Phase 3, exactly as planned.
