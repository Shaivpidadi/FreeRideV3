# FreeRide v3 — Design Brief & Plan

> **Status:** Draft. Living document. Iterate until the design feels real, then start writing code.
>
> **Where development happens:** Branch `v3` on the existing `Shaivpidadi/FreeRide` repo (local only, unpushed) until abstractions stabilize. Migration to a new private GitHub repo deferred to Phase 3.

---

## 1. What v3 is, in one sentence

A **provider-agnostic, consumer-agnostic engine** for keeping any AI agent runtime running on any free-tier inference provider, indefinitely and unattended.

## 2. What v2 is (and stays, untouched)

- Single-purpose CLI: keeps **OpenClaw** running on **OpenRouter** free models.
- ~1.1 KLOC of Python.
- Discovers, ranks, live-tests, and writes 27 chat-shaped free models into `~/.openclaw/openclaw.json`.
- Optional watcher daemon that recovers from "every model in the chain is rate-limited" deadlocks the agent itself can't escape.
- Strongly opinionated: free-only, no paid models, no auto-installed OS services.

That's the **showcase**. v3 generalizes the engine underneath while leaving the showcase unchanged.

## 3. Why now

### Pull from open issues
- **#14 — NVIDIA NIM support.** Different provider, different free-tier shape (credits + caps, not `:free` suffix).
- **#11 — Hermes agent support.** Different consumer, different config format.

### Push from market shape
- Free-tier AI is a moving target spread across **6+ providers**: OpenRouter, NVIDIA NIM, Groq, Cloudflare Workers AI, Hugging Face Inference, Together (limited free tier), DeepInfra trial, etc. No single provider is reliably "always free"; the resilience lives in the **union**.
- Multiple agent runtimes face the same orchestration problem: OpenClaw, Hermes, Aider, Continue, OpenCode, llama.cpp, LM Studio. Most either hard-code one provider or punt the problem entirely.
- The OpenClaw + OpenRouter coupling is implementation detail, not the product. The actual product is **"unattended free AI."**

## 4. v2 lessons that must carry forward

These are baked-in, non-negotiable for v3:

1. **Live-test before writing.** Metadata-based ranking picked image/audio-gen models as top-ranked chat models in v2 testing. Only an actual `/chat/completions` probe revealed it. Every config write in v3 must go through a live test of the chosen primary.
2. **Verify payload against the actual consumer, not inferred behavior.** v2 shipped two wrong commits (`c3f4d1d`, `0df30f4`) because we inferred the OpenClaw config format from existing values without checking with OpenClaw's docs. v3's plugin contract must include explicit conformance tests against the real consumer.
3. **"Free" is not one signal.** OpenRouter has both `pricing.prompt == 0` and `:free` suffix, and they sometimes disagree. Other providers expose totally different signals or none at all. Each provider needs its own free-detection logic.
4. **Don't auto-install OS-level services.** Trust cost > convenience. v3's watcher must remain user-invoked. No launchd plists, systemd units, cron entries, or shell-profile edits — ever.
5. **Stamp every external request with attribution headers.** OpenRouter's App Activity page is the only ground-truth telemetry FreeRide has, and it was missing two-thirds of the actual traffic until commit `e673633`. v3 keeps the centralized header helper pattern.
6. **The agent can't rescue itself.** Recovery has to live outside the inference loop. The daemon model is non-negotiable — calling `freeride rotate` from inside a stuck agent is a chicken-and-egg deadlock.

## 5. The hard part — heterogeneity

v3 isn't a refactor. It's a redesign that has to absorb real heterogeneity at both ends.

### 5.1 Across providers — what "free" actually means

| Provider | Free-detection signal | Quota-exhaustion signal | Probe convention |
|---|---|---|---|
| **OpenRouter** | `pricing.prompt == 0` OR `:free` suffix | HTTP 429 with `code: 429` and `provider_name` in error | `/chat/completions` with `max_tokens: 5`, prompt `"Hi"` |
| **NVIDIA NIM** | Free credit tiers, daily caps; not in model object | HTTP 429 (discovered by hitting it) | TBD — some NIM endpoints reject tiny calls |
| **Groq** | Specific allowlist of models, hard daily token caps per key | HTTP 429 with quota header | `/openai/v1/chat/completions`, OpenAI-compatible |
| **Cloudflare Workers AI** | Free up to N "neurons" per day (synthetic unit, not tokens) | Custom 429 shape | `/ai/run/<model>` |
| **Hugging Face Inference** | Per-model rate-limited free tier; occasional outright rejection | HTTP 503 / queue messages | `/models/<id>` with custom payload shape |

There is **no uniform predicate** for "is this free." Each provider needs its own:
- Free-model **discovery** method.
- Quota-**exhaustion signal** parser.
- **Probe** convention (some providers don't allow tiny calls, some count probe tokens against quota).
- **Auth header** convention.
- **Attribution** mechanism (OpenRouter has it; most don't).

### 5.2 Across consumers — what "configure a model" means

| Consumer | Config path | Format | Picked-up by |
|---|---|---|---|
| **OpenClaw** | `~/.openclaw/openclaw.json` | `<provider>/<model>` (provider segment is OpenClaw's routing prefix) | `openclaw gateway restart` |
| **Hermes** | TBD (issue #11 body is one line) | TBD | TBD |
| **Aider** | `.aider.conf.yml` + `OPENAI_API_BASE` env | Single model spec, OpenAI-compatible URL | Restart |
| **Continue** | `~/.continue/config.json` | Multi-model array per role (chat/edit/autocomplete) | Hot reload |
| **OpenCode** | YAML provider config | Provider object + model list | Restart |
| **llama.cpp / LM Studio** | Local model swap, no remote API | N/A — out of scope for now | N/A |

Each consumer needs:
- A **config path** discovery method.
- A read/write contract that **preserves unrelated keys** (single biggest user trust requirement — never clobber their gateway settings, channels, etc.).
- A **model ref format** rule (e.g., OpenClaw's routing prefix; Aider's bare ID).
- A **change-pickup** mechanism (restart command, hot reload, file watch).

This is where the real design effort goes.

## 6. Proposed architecture

### 6.1 Package layout (illustrative — names TBD)

```
freeride/                     SDK package — name TBD
├── core/
│   ├── interfaces.py         Provider / Consumer ABCs + dataclasses
│   ├── orchestrator.py       rank, probe, choose primary + fallbacks
│   ├── watcher.py            provider/consumer-agnostic loop
│   └── config_io.py          atomic read/write, preserves unrelated keys
├── providers/
│   ├── openrouter.py         reference (port of v2 logic)
│   ├── nvidia_nim.py
│   └── groq.py               (later)
├── consumers/
│   ├── openclaw.py           reference (port of v2 logic)
│   ├── hermes.py
│   └── aider.py              (later)
├── cli/
│   ├── freeride.py           user-facing CLI
│   └── watcher.py            freeride-watcher entry point
└── plugins/
    └── discovery.py          Python entrypoint loader
```

### 6.2 Core interfaces (sketch)

```python
class Provider(Protocol):
    name: str
    auth_env_var: str

    def list_free_models(self, key: str) -> list[Model]: ...
    def probe(self, model_id: str, key: str) -> ProbeResult: ...
    def quota_exhausted(self, response) -> bool: ...
    def auth_header(self, key: str) -> dict[str, str]: ...
    def attribution_headers(self) -> dict[str, str]: ...

class Consumer(Protocol):
    name: str
    config_path: Path

    def read(self) -> ConsumerConfig: ...
    def write(self, primary: ModelRef, fallbacks: list[ModelRef]) -> None: ...
    def format_model_ref(self, provider: Provider, model_id: str) -> str: ...
    def parse_model_ref(self, stored: str) -> tuple[str, str]: ...   # (provider_name, api_id)
    def restart_hint(self) -> str: ...

@dataclass
class Model:
    api_id: str                       # provider's native ID
    provider: str                     # provider.name
    context_length: int
    output_modalities: list[str]      # ["text"], ["text","audio"], …
    supported_parameters: list[str]
    raw: dict                         # provider-specific extras

@dataclass
class ProbeResult:
    ok: bool
    error: Optional[str]              # "rate_limit"|"model_not_found"|"quota_exhausted"|"timeout"|…
    latency_ms: int
```

### 6.3 Orchestrator responsibilities (provider/consumer-agnostic)

1. Aggregate `list_free_models()` across all configured providers; dedupe by `(provider, api_id)`.
2. Filter to chat-capable (text-output) models.
3. Rank by score (context length, capability count, recency, provider trust).
4. Probe candidates live; skip dead.
5. Build primary + fallback chain (smart-router-style entries first if the provider supports it).
6. Hand off to `Consumer.write(primary, fallbacks)`.

### 6.4 Watcher responsibilities (loop, provider/consumer-agnostic)

1. Periodically `Consumer.read()` → extract current primary's `(provider, api_id)`.
2. Find the matching `Provider`, call `provider.probe(api_id, key)`.
3. On failure → run orchestrator for that consumer, write new chain.
4. Persistent state: rotation count, last-rotation reason, last-checked-at. Atomic writes.
5. Stop on `SIGINT` / `SIGTERM`.

### 6.5 Plugin discovery

Third-party plugins ship as their own pip packages and register via Python entrypoints:

```toml
[project.entry-points."freeride.providers"]
anthropic = "freeride_provider_anthropic:AnthropicProvider"

[project.entry-points."freeride.consumers"]
aider = "freeride_consumer_aider:AiderConsumer"
```

`freeride.plugins.discovery` enumerates these on startup; CLI auto-discovers without core code changes.

## 7. First-cut scope (validation cut, NOT v1.0)

**Goal:** prove the abstractions are sound before publishing them as a contract third parties build on.

1. **Refactor v2 logic** into `core/` + `providers/openrouter.py` + `consumers/openclaw.py`. CLI behavior identical to v2 from the user's perspective.
2. **Add one new provider — NVIDIA NIM** (issue #14 motivator). Forces the "what does free mean here" question for real.
3. **Add one new consumer — Hermes** (issue #11 motivator), or a reasonable proxy if Hermes turns out to be ill-defined.
4. **Tests:**
   - Unit tests per Provider / Consumer.
   - Cross-product integration: 2 providers × 2 consumers; orchestrator must not care which combination it runs.
5. **Migration shim:** existing `freeride auto` keeps working with no user-facing CLI change.

**Validation criterion:** if 2 + 1 + 1 work cleanly with the proposed interfaces, the seams are real. If they fight each other constantly, we found the wrong seams *before* shipping a public SDK contract — which is exactly when we want to find that out.

## 8. Repo & product strategy

### 8.1 Now (during local dev)
- Branch `v3` on existing FreeRide repo. Local only. No push.

### 8.2 When abstractions stabilize (Phase 2 done)
- Spin up a **new private GitHub repo**.
- Push v3 code there as the new "engine" project.
- FreeRide repo stays as-is on v2; bug fixes only.

### 8.3 When we go public
Two coherent endings, decision deferred until we know if anyone besides us wants to ship their own free-tier orchestrator on top of this engine:

- **(a) Replace.** New repo eventually becomes the public face; FreeRide's `main` rebases or migrates onto v3. Single brand, single audience, single product. **Cost:** lose the focused "free AI for OpenClaw" pitch.
- **(b) Two products.** FreeRide stays the OpenClaw skill. New repo is the SDK (name TBD). FreeRide imports the SDK. **Cost:** grow a new audience for the SDK; two repos to maintain.

If at least two real third parties want to ship on the engine → **(b)**. If only we'll ever care → **(a)**.

## 9. Open questions (decide before implementing in earnest)

1. **Naming.** "FreeRide" generalizes (still riding free) but is tightly associated with OpenClaw. Candidates: `freerunner`, `multifree`, `agentfree`, keep `freeride`. Don't decide until a prototype works.
2. **What is "Hermes"** in issue #11? The Nous Hermes model on OpenRouter? Or some agent framework with that name? Need to ask the reporter or commit to a definition before designing `consumers/hermes.py`.
3. **Plugin contract versioning.** SemVer'd interface (`Provider_v1`, `Provider_v2`) or unversioned + best-effort? Versioning costs maintenance, ages well; unversioned is cheap, breaks every contributor's plugin on contract changes.
4. **Migration for existing v2 users.** Hard break (v3 is a new package, v2 keeps existing) or in-place upgrade with shims? In-place is nicer but constrains v3 to v2's CLI surface forever.
5. **SDK consumers we want from day one.** Beyond OpenClaw, which agent runtime authors do we want adopting `pip install freeride-core` to handle their free-model story? If we can name two real ones now, the abstraction effort pays off; if we can't, v3 risks being internal cleanup with no external pull.
6. **Telemetry.** OpenRouter's App Activity page only sees OpenRouter traffic. For multi-provider, do we want our own opt-in telemetry to know which providers are working in the wild? Project memory `project_no_auto_service_install` argues against anything that smells like phoning home; needs careful thought.
7. **Quota visibility.** Should the SDK expose a `quota_state(key)` query so consumers can be honest with users about "you have ~120 requests left today"? Several providers expose this; some don't.

## 10. Out of scope (explicit non-goals)

- **Paid models, ever.** Free-only is the project memory rule. No "premium fallback," no "graceful upgrade path" features.
- **Auto-installing services.** No launchd plists, systemd units, cron entries, shell-profile edits. Documented `nohup`/launchd patterns only.
- **Becoming a routing proxy.** v3 writes config and probes models. It does not sit in the request path. Routing is the consumer's job (OpenClaw, Aider, etc.).
- **General-purpose model marketplace.** We curate free, chat-shaped models. Not image-gen, not audio-gen, not embeddings (yet).
- **Rate-limit prediction / quota arbitrage.** We probe and react. We don't model provider rate limits.
- **Local-only consumers** (llama.cpp, LM Studio) **for now.** They have no remote API to probe; the orchestrator's value is thin. Revisit if there's demand.

## 11. Roadmap (phased; rough estimates)

### Phase 0 — Plan stable (this week)
- This document iterated until the design feels real.
- Open questions answered enough to start writing code.

### Phase 1 — Refactor in place (1–2 weeks)
- v2 logic refactored into `core/` + `providers/openrouter.py` + `consumers/openclaw.py`.
- CLI behavior identical to v2.
- All existing v2 tests pass.
- Boring, mechanical part. No external behavior change.

### Phase 2 — Validation cut (2–3 weeks)
- Add `providers/nvidia_nim.py` (probably the harder of the two).
- Add `consumers/hermes.py` (or whichever Hermes definition lands).
- Cross-product integration test.
- Iterate abstractions based on what hurts.

### Phase 3 — Decision point
- If abstractions are clean: spin up private repo, port branch over, decide naming.
- If abstractions fight each other: regroup. Probably the seams are wrong. Better to learn this in Phase 2 than Phase 4.

### Phase 4 — Public SDK (after Phase 3 succeeds)
- Plugin contract documented and versioned.
- 1–2 third-party plugins ship (likely written by us first to validate ergonomics).
- Public release of the SDK repo.
- FreeRide-the-skill refactored into a thin wrapper around the SDK (or replaced, depending on Phase 3 decision).

## 12. Glossary

- **Provider** — a source of free AI models (OpenRouter, NVIDIA NIM, …).
- **Consumer** — an agent runtime that uses models (OpenClaw, Hermes, …).
- **Model ref** — a string identifying a model in some consumer's config format.
- **Routing prefix** — a leading segment in a model ref that tells the consumer which provider to dispatch to (e.g., OpenClaw's `openrouter/`).
- **Live probe** — a real `chat/completions` request with a tiny prompt and `max_tokens: 5`, used to verify model availability.

## 13. References

### v2 issues that motivate v3
- **#11** — Hermes agent support (consumer-generalization driver).
- **#12** — Fallbacks routing wrong provider (root cause: bare model IDs; fixed in `c3f4d1d` but informs v3's consumer-format design).
- **#14** — NVIDIA NIM support (provider-generalization driver).

### Carry-forward principles (from the project's auto-memory)
- `project_free_only.md` — free-models-only constraint, no paid models.
- `project_no_auto_service_install.md` — no OS-level service auto-install.
- `feedback_verify_payload_before_committing.md` — verify config/protocol changes against the real consumer, not against inferred behavior.
- `feedback_no_claude_coauthor.md` — commit hygiene.
