# FreeRide V3 — Execution Plan

> Companion to `PLAN_GATEWAY.md`. The design plan describes **what** we're building.
> This document describes **exactly how**, in commit-sized increments, end-to-end.
> Self-contained: when executing, this is the source of truth.

---

## Execution rules

1. **Commit per task.** Every numbered task ends with exactly one `git commit`. Never bundle.
2. **E2E test per feature.** Each feature defines a concrete pass/fail script. The next feature does not start until the previous feature's E2E test passes.
3. **No co-author lines, ever.** Per `feedback_no_claude_coauthor.md`. No `Co-Authored-By:` in any commit.
4. **Commit message style.** Imperative subject ≤ 72 chars, lowercase. Body optional, ≤ 72 char/line. No emoji.
5. **Halt on E2E failure.** Don't patch over a failure to make it pass. Diagnose root cause; fix; re-run.
6. **Halt on phase-gate failure.** Each phase has a gate. Failure means re-cut the phase, not skip to the next.
7. **Daytona budget = $100.** Track cumulative cost. Halt at $80 and report status before continuing.
8. **API keys via env.** Never commit secrets. `.env.example` only. Real keys live in Daytona env vars.
9. **One branch per phase.** Branch off `main`: `phase-1-refactor`, `phase-2-gateway`, etc. Merge to `main` only when phase gate passes.
10. **Force-push only on personal branches.** Never on `main`.

## Glossary

- **Phase** — multi-week milestone. Ends with a phase gate (E2E + criterion check).
- **Feature** — one cohesive capability. Ends with an E2E test.
- **Task** — one commit's worth of work. 30 min – 2 hours.
- **Subtask** — smallest verifiable step. Multiple subtasks roll up to one task → one commit.

---

# PHASE 1 — Library refactor (no behavior change)

**Goal:** refactor v2's logic into the `freeride/` package layout. CLI behavior identical to v2 from the user's perspective. No external API.

**Phase gate (must pass before Phase 2):**
- All v2 CLI commands (`auto`, `list`, `switch`, `status`, `refresh`, `fallbacks`, `rotate`) produce byte-identical OpenClaw config to a v2 baseline run, given the same OpenRouter key.
- `freeride-watcher --once` behaves identically to v2.

---

## Feature 1.1 — Package skeleton

**E2E test 1.1:** From a fresh clone:
```bash
pip install -e . && \
python -c "import freeride, freeride.core, freeride.providers, freeride.server, freeride.cli" && \
freeride --help
```
Exit code 0; help text printed.

### Task 1.1.1 — Initialize package layout
- 1.1.1.1: Create `freeride/__init__.py` with `__version__ = "0.3.0-dev"`
- 1.1.1.2: Create `freeride/core/__init__.py` (empty)
- 1.1.1.3: Create `freeride/providers/__init__.py` (empty)
- 1.1.1.4: Create `freeride/server/__init__.py` (empty)
- 1.1.1.5: Create `freeride/cli/__init__.py` (empty)
- 1.1.1.6: Create `tests/__init__.py` (empty)

**Commit:** `scaffold freeride package layout`

### Task 1.1.2 — Migrate to pyproject.toml
- 1.1.2.1: Create `pyproject.toml` (PEP 621 metadata, name, version, deps)
- 1.1.2.2: Runtime deps: `httpx>=0.27`, `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `pydantic>=2.7`
- 1.1.2.3: Dev deps: `pytest`, `pytest-asyncio`, `pytest-httpx`, `ruff`
- 1.1.2.4: `[project.scripts] freeride = "freeride.cli.main:main"`, `freeride-watcher = "freeride.cli.watcher:main"`
- 1.1.2.5: Delete `setup.py` (redundant under PEP 621)

**Commit:** `migrate to pyproject.toml; declare httpx + fastapi deps`

### Task 1.1.3 — Stub CLI entry points
- 1.1.3.1: Create `freeride/cli/main.py` with `def main(): print("freeride v0.3.0-dev")`
- 1.1.3.2: Create `freeride/cli/watcher.py` with `def main(): print("freeride-watcher v0.3.0-dev")`
- 1.1.3.3: Add `--help` argparse stub to both

**Commit:** `add CLI entry stubs to satisfy console_scripts`

---

## Feature 1.2 — Core data types

**E2E test 1.2:** `pytest tests/test_core_types.py` passes; types import without circular references.

### Task 1.2.1 — Define Model and ProbeResult
- 1.2.1.1: Create `freeride/core/types.py`
- 1.2.1.2: Define `Model` dataclass: `api_id`, `provider`, `context_length`, `output_modalities`, `supported_parameters`, `raw`
- 1.2.1.3: Define `ProbeResult` dataclass: `ok`, `error`, `latency_ms`

**Commit:** `add Model and ProbeResult core types`

### Task 1.2.2 — Define ChatRequest / ChatResponse / ChatStreamEvent
- 1.2.2.1: Define `ChatRequest` (Pydantic model mirroring OpenAI `/v1/chat/completions` request schema; messages, model, max_tokens, stream, tools, response_format, …)
- 1.2.2.2: Define `ChatResponse` (mirrors OpenAI response shape)
- 1.2.2.3: Define `ChatStreamEvent` (SSE event shape)

**Commit:** `add ChatRequest/Response/StreamEvent OpenAI-compatible schemas`

### Task 1.2.3 — Define ErrorKind enum
- 1.2.3.1: `freeride/core/errors.py` with `ErrorKind` enum: `OK`, `RATE_LIMIT`, `QUOTA_EXHAUSTED`, `MODEL_NOT_FOUND`, `UNAVAILABLE`, `TIMEOUT`, `AUTH`, `UNKNOWN`
- 1.2.3.2: Helper `is_retryable(kind: ErrorKind) -> bool` (RATE_LIMIT and UNAVAILABLE only)

**Commit:** `add ErrorKind enum and retryable classification helper`

### Task 1.2.4 — Tests for core types
- 1.2.4.1: `tests/test_core_types.py` — round-trip JSON for each Pydantic model
- 1.2.4.2: Test all ErrorKind values; test `is_retryable` mapping

**Commit:** `add tests for core types and error classification`

---

## Feature 1.3 — Provider Protocol contract

**E2E test 1.3:** `pytest tests/test_provider_protocol.py` — a NoopProvider implementing the contract passes a runtime `isinstance` check; `api_version == 1`.

### Task 1.3.1 — Define Provider Protocol
- 1.3.1.1: `freeride/core/provider.py` with `Provider` Protocol
- 1.3.1.2: Required attrs: `name: str`, `api_version: int = 1`
- 1.3.1.3: Required methods: `list_free_models`, `probe`, `forward_chat` (async), `forward_chat_stream` (async iterator), `classify_error`, `retry_after_hint`, `auth_header`, `attribution_headers`

**Commit:** `define Provider Protocol with api_version frozen at 1`

### Task 1.3.2 — Conformance test fixture
- 1.3.2.1: `tests/conformance/test_provider_conformance.py` parameterized fixture
- 1.3.2.2: NoopProvider implementation in `tests/fixtures/noop_provider.py`
- 1.3.2.3: Conformance asserts: all methods callable; `api_version == 1`; `attribution_headers()` returns dict

**Commit:** `add Provider conformance test fixture`

---

## Feature 1.4 — OpenRouter provider port

**E2E test 1.4:** With a valid `OPENROUTER_API_KEY`, `OpenRouterProvider().list_free_models(key)` returns ≥ 20 models; `provider.probe("openrouter/free", key)` returns `ProbeResult(ok=True, ...)`.

### Task 1.4.1 — Move free-detection logic
- 1.4.1.1: Create `freeride/providers/openrouter.py`
- 1.4.1.2: Port `_is_chat_model` from v2 `main.py`
- 1.4.1.3: Port `filter_free_models` (dual signal: `pricing.prompt == 0` OR `:free`)

**Commit:** `port openrouter free-model detection from v2`

### Task 1.4.2 — Implement list_free_models
- 1.4.2.1: Port `fetch_all_models` (rotating multi-key)
- 1.4.2.2: Convert raw API response → `Model` dataclass
- 1.4.2.3: Wire to OpenRouter `/api/v1/models` via httpx (sync method, async wrapper next task)

**Commit:** `port openrouter list_free_models`

### Task 1.4.3 — Implement probe
- 1.4.3.1: Port `_test_model` from v2
- 1.4.3.2: Map raw error responses → `ErrorKind`
- 1.4.3.3: Return `ProbeResult` with latency

**Commit:** `port openrouter probe with ErrorKind classification`

### Task 1.4.4 — Implement classify_error and retry_after_hint
- 1.4.4.1: Centralize the v2 string-classification logic into `classify_error`
- 1.4.4.2: Read `retry-after` header where present; OpenRouter rarely sends it — return `None` otherwise

**Commit:** `port openrouter error classification and retry-after`

### Task 1.4.5 — Implement auth_header and attribution_headers
- 1.4.5.1: `auth_header(key)` → `{"Authorization": f"Bearer {key}"}`
- 1.4.5.2: `attribution_headers()` → `{"HTTP-Referer": ..., "X-Title": ...}` (port v2 constants)

**Commit:** `port openrouter auth and attribution headers`

### Task 1.4.6 — Stub forward_chat (async) for later phase
- 1.4.6.1: `async def forward_chat(...)` — `raise NotImplementedError("Phase 2")`
- 1.4.6.2: `async def forward_chat_stream(...)` — `raise NotImplementedError("Phase 3")`

**Commit:** `stub forward_chat methods for Phase 2/3`

### Task 1.4.7 — Tests
- 1.4.7.1: `tests/providers/test_openrouter.py` — mock httpx, assert `list_free_models` parses correctly
- 1.4.7.2: Probe test with mocked 200, 429, 503, 401 responses; assert correct `ErrorKind`
- 1.4.7.3: Conformance test passes for `OpenRouterProvider`

**Commit:** `add openrouter provider tests`

---

## Feature 1.5 — httpx migration

**E2E test 1.5:** `grep -r "import requests" freeride/` returns nothing. All HTTP traffic flows through httpx. `pytest tests/` passes.

### Task 1.5.1 — Replace requests in OpenRouter provider
- 1.5.1.1: Swap `requests.get/post` → `httpx.Client.get/post`
- 1.5.1.2: Convert exception types: `requests.Timeout` → `httpx.TimeoutException`, etc.
- 1.5.1.3: Run unit tests; ensure parity

**Commit:** `migrate openrouter provider from requests to httpx`

### Task 1.5.2 — Async surfaces for Phase 2
- 1.5.2.1: Add `async def list_free_models_async` and `async def probe_async` using `httpx.AsyncClient`
- 1.5.2.2: Sync methods delegate to async via `asyncio.run` for CLI use

**Commit:** `add async httpx surfaces alongside sync wrappers`

### Task 1.5.3 — Drop requests from pyproject.toml
- 1.5.3.1: Remove `requests` from runtime deps
- 1.5.3.2: Confirm `pip install -e .` still installs cleanly

**Commit:** `remove requests dependency`

---

## Feature 1.6 — State, cooldown, atomic writes

**E2E test 1.6:** Kill the process mid-write; `~/.freeride/cooldown.json` is either empty or valid JSON, never corrupted. Cooldown survives process restart.

### Task 1.6.1 — Atomic write helper
- 1.6.1.1: Create `freeride/core/state.py`
- 1.6.1.2: `def atomic_write(path: Path, content: str)` — temp + `os.replace`
- 1.6.1.3: Tests: kill mid-write simulation via `pytest`'s `monkeypatch`

**Commit:** `add core/state.py with atomic_write helper`

### Task 1.6.2 — Persistent key cooldown
- 1.6.2.1: `freeride/core/cooldown.py` — `KeyCooldown` class, persisted to `~/.freeride/cooldown.json`
- 1.6.2.2: Methods: `mark_rate_limited(key, provider)`, `is_in_cooldown(key, provider) -> bool`, `available_keys(provider) -> list[str]`
- 1.6.2.3: TTL = 120s (matches v2)

**Commit:** `add persistent KeyCooldown across restarts`

### Task 1.6.3 — Migrate v2 `save_openclaw_config` to atomic_write
- 1.6.3.1: Create `freeride/consumers/openclaw_writer.py` (still single consumer for now)
- 1.6.3.2: Use `atomic_write` (fixes a latent v2 bug)

**Commit:** `fix openclaw config write to be atomic`

### Task 1.6.4 — Tests
- 1.6.4.1: `tests/test_state.py` — atomic write does not corrupt
- 1.6.4.2: `tests/test_cooldown.py` — TTL behavior, restart persistence

**Commit:** `add tests for state and cooldown`

---

## Feature 1.7 — CLI parity wrappers

**E2E test 1.7:** Side-by-side: run `freeride auto` (v3) and v2's `freeride auto` against same OpenClaw config + same OpenRouter key. Resulting JSON files match byte-for-byte.

### Task 1.7.1 — `freeride auto`
- 1.7.1.1: Port `cmd_auto` from v2 to `freeride/cli/cmd_auto.py`
- 1.7.1.2: Use `OpenRouterProvider` + `openclaw_writer`
- 1.7.1.3: `argparse` matches v2 flag set

**Commit:** `port freeride auto to v3 layout`

### Task 1.7.2 — `freeride list`
- 1.7.2.1: Port `cmd_list`

**Commit:** `port freeride list`

### Task 1.7.3 — `freeride switch`
- 1.7.3.1: Port `cmd_switch`

**Commit:** `port freeride switch`

### Task 1.7.4 — `freeride status`
- 1.7.4.1: Port `cmd_status`

**Commit:** `port freeride status`

### Task 1.7.5 — `freeride refresh`
- 1.7.5.1: Port `cmd_refresh`

**Commit:** `port freeride refresh`

### Task 1.7.6 — `freeride fallbacks`
- 1.7.6.1: Port `cmd_fallbacks`

**Commit:** `port freeride fallbacks`

### Task 1.7.7 — `freeride rotate`
- 1.7.7.1: Port `cmd_rotate` and the `rotate()` helper

**Commit:** `port freeride rotate`

### Task 1.7.8 — `freeride-watcher`
- 1.7.8.1: Port `watcher.py` to `freeride/cli/watcher.py`
- 1.7.8.2: Use new state/cooldown modules

**Commit:** `port freeride-watcher to v3 layout`

---

## Feature 1.8 — v2 parity test suite

**E2E test 1.8 (= phase gate):** A reproducible script `tests/parity/run_parity.sh` runs every v2 CLI command against a fixed OpenClaw config + fixed OpenRouter key (or recorded fixtures), captures output, and `diff`s against a v2 baseline. Zero diff.

### Task 1.8.1 — Record v2 baseline
- 1.8.1.1: Run v2 against a known-good OpenRouter key inside Daytona
- 1.8.1.2: Capture all CLI outputs to `tests/parity/baseline/`

**Commit:** `record v2 baseline outputs for parity tests`

### Task 1.8.2 — Parity runner
- 1.8.2.1: `tests/parity/run_parity.sh` — runs v3 commands, captures outputs
- 1.8.2.2: `diff -r tests/parity/baseline tests/parity/v3_actual` must be empty

**Commit:** `add v2-vs-v3 parity test runner`

### Task 1.8.3 — Phase 1 gate run
- 1.8.3.1: Execute parity runner; resolve any drift
- 1.8.3.2: Tag commit `v0.3.0-phase1`

**Commit:** `tag phase 1 complete; v2 parity verified`

---

# PHASE 2 — Minimal gateway

**Goal:** `freeride serve` exposes OpenAI-compatible HTTP. One provider, multi-key, mid-request retry on 429/auth-failure. Non-streaming only.

**Phase gate:** `OPENAI_API_BASE=http://localhost:11343/v1 OPENAI_API_KEY=any aider --message "hello"` round-trips successfully against a real Aider install. No Aider patches.

---

## Feature 2.1 — FastAPI server skeleton

**E2E test 2.1:** `freeride serve --port 11343` starts; `curl http://localhost:11343/health` returns 200; SIGTERM stops cleanly.

### Task 2.1.1 — Server module
- 2.1.1.1: `freeride/server/app.py` — FastAPI instance
- 2.1.1.2: `GET /health` → `{"ok": true}`
- 2.1.1.3: Lifespan handler: startup (load providers), shutdown (flush stats)

**Commit:** `add FastAPI server skeleton with /health`

### Task 2.1.2 — `freeride serve` CLI command
- 2.1.2.1: `freeride/cli/cmd_serve.py`
- 2.1.2.2: Args: `--port` (default 11343), `--host` (default 127.0.0.1), `--verbose` (default False)
- 2.1.2.3: Refuses to start if port in use (don't auto-pick)

**Commit:** `add freeride serve command with port-collision guard`

### Task 2.1.3 — Logging policy
- 2.1.3.1: Default: log only request/response timing + status, never bodies
- 2.1.3.2: `--verbose` opt-in: log truncated bodies (first 200 chars), explicit warning at startup

**Commit:** `add no-log-by-default logging policy`

### Task 2.1.4 — Tests
- 2.1.4.1: `tests/server/test_app.py` — TestClient hits `/health`
- 2.1.4.2: Test port-collision refusal

**Commit:** `add server skeleton tests`

---

## Feature 2.2 — `GET /v1/models`

**E2E test 2.2:** `curl http://localhost:11343/v1/models` returns OpenAI-format model list with ≥ 20 free OpenRouter models, ranked.

### Task 2.2.1 — Implement endpoint
- 2.2.1.1: `freeride/server/routes/models.py`
- 2.2.1.2: Calls `OpenRouterProvider.list_free_models_async`
- 2.2.1.3: Reuses 6h cache from v2 (`freeride/core/cache.py`)
- 2.2.1.4: Output shape: OpenAI-compatible `{"object": "list", "data": [...]}`

**Commit:** `add GET /v1/models endpoint`

### Task 2.2.2 — Cache layer
- 2.2.2.1: `freeride/core/cache.py` — TTL cache, atomic write
- 2.2.2.2: 6h default; `?refresh=true` query param bypasses

**Commit:** `add ttl model cache with refresh override`

### Task 2.2.3 — Tests
- 2.2.3.1: Mock provider; assert response shape, ranking
- 2.2.3.2: Cache hit/miss behavior

**Commit:** `add /v1/models endpoint tests`

---

## Feature 2.3 — `POST /v1/chat/completions` (non-streaming)

**E2E test 2.3:**
```bash
curl -sX POST http://localhost:11343/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/free","messages":[{"role":"user","content":"hi"}],"max_tokens":10}' \
  | jq -e '.choices[0].message.content'
```
Returns non-empty content.

### Task 2.3.1 — Forward-chat non-streaming in OpenRouter provider
- 2.3.1.1: Implement `forward_chat` (replace NotImplementedError)
- 2.3.1.2: Pass through all OpenAI fields verbatim
- 2.3.1.3: Stamp auth + attribution headers per request

**Commit:** `implement openrouter forward_chat (non-streaming)`

### Task 2.3.2 — Endpoint handler
- 2.3.2.1: `freeride/server/routes/chat.py`
- 2.3.2.2: Validates request via Pydantic
- 2.3.2.3: Hands off to resolver (next feature)

**Commit:** `add POST /v1/chat/completions handler`

### Task 2.3.3 — Tests
- 2.3.3.1: Mock provider; assert pass-through of unusual fields (tools, response_format) — round-trip

**Commit:** `add chat-completions handler tests`

---

## Feature 2.4 — Health tracker

**E2E test 2.4:** Send 10 successful + 3 rate-limited responses to a (provider, model, key) tuple; `freeride status` reports tuple's success rate as 10/13.

### Task 2.4.1 — In-process health state
- 2.4.1.1: `freeride/core/health.py` — `HealthTracker` class
- 2.4.1.2: Records per-(provider, model, key): success count, error count, last latency, last_error_at
- 2.4.1.3: Bounded LRU (default 1000 tuples)

**Commit:** `add HealthTracker with bounded LRU`

### Task 2.4.2 — Wire into request path
- 2.4.2.1: Every forward_chat success/failure → tracker.record()

**Commit:** `wire health tracker into request path`

### Task 2.4.3 — Tests
- 2.4.3.1: Record + read; LRU eviction; survives single process

**Commit:** `add health tracker tests`

---

## Feature 2.5 — Persistent multi-provider key pool

**E2E test 2.5:** Configure 3 OpenRouter keys; rate-limit one; gateway uses the other two for next request. Verify cooldown survives process restart.

### Task 2.5.1 — Generalize KeyCooldown to multi-provider
- 2.5.1.1: Extend `KeyCooldown` from Phase 1 to namespace by provider name
- 2.5.1.2: API: `available_keys(provider) -> list[str]`

**Commit:** `extend KeyCooldown to namespace by provider`

### Task 2.5.2 — Key loader
- 2.5.2.1: `freeride/core/keys.py` — load from env vars per provider
- 2.5.2.2: OpenRouter: `OPENROUTER_API_KEY` (single or JSON array, like v2)
- 2.5.2.3: NIM: `NVIDIA_API_KEY` — same format (used in Phase 3)

**Commit:** `add per-provider key loader from env`

### Task 2.5.3 — Tests
- 2.5.3.1: Single-key, multi-key, JSON-array forms parse correctly

**Commit:** `add key loader tests`

---

## Feature 2.6 — Resolver + retry policy

**E2E test 2.6:** Configure 2 OpenRouter keys; manually invalidate one (mid-flight 401 simulated); single client request still succeeds. No 401 visible to client.

### Task 2.6.1 — Resolver
- 2.6.1.1: `freeride/server/resolver.py` — `resolve(request) -> (provider, model_id, key)`
- 2.6.1.2: Strategy: prefer the requested model on the most-healthy key; fallback to next-ranked free model on next-healthy key

**Commit:** `add resolver: provider+model+key selection by health`

### Task 2.6.2 — Retry loop
- 2.6.2.1: `freeride/server/retry.py` — `async def call_with_retry(request)`
- 2.6.2.2: On `RATE_LIMIT` / `AUTH`: mark key, pick next, retry
- 2.6.2.3: On `MODEL_NOT_FOUND`: pick next-ranked model
- 2.6.2.4: On `UNAVAILABLE`: bounded retry (max 2)
- 2.6.2.5: After N total failures: return upstream error to client

**Commit:** `add request-time retry loop with ErrorKind classification`

### Task 2.6.3 — Tests
- 2.6.3.1: Simulated 401 on first key → success on second
- 2.6.3.2: All keys exhausted → clean error (not crash)

**Commit:** `add resolver+retry tests`

---

## Feature 2.7 — Local stats writer

**E2E test 2.7:** After 5 successful requests, `freeride status` shows: `Tokens served: <nonzero>`, `Requests: 5`, `Top provider: openrouter`.

### Task 2.7.1 — Stats accumulator
- 2.7.1.1: `freeride/core/stats.py` — `Stats` class persisted to `~/.freeride/stats.json`
- 2.7.1.2: Records: total_tokens, request_count, per-provider counts, uptime
- 2.7.1.3: Atomic write every 60s + on shutdown

**Commit:** `add local Stats accumulator persisted to ~/.freeride`

### Task 2.7.2 — Wire into request path
- 2.7.2.1: After every successful response, increment counters

**Commit:** `wire stats into successful request path`

### Task 2.7.3 — `freeride status` shows them
- 2.7.3.1: Extend `cmd_status` to load and print stats

**Commit:** `extend freeride status with stats output`

### Task 2.7.4 — Tests
- 2.7.4.1: Increment, persist, reload

**Commit:** `add stats tests`

---

## Phase 2 gate

### Task 2.G — Aider integration test
- 2.G.1: Install Aider in Daytona
- 2.G.2: `OPENAI_API_BASE=http://localhost:11343/v1 OPENAI_API_KEY=any aider --message "what is 2+2"`
- 2.G.3: Verify response content makes sense
- 2.G.4: Tag `v0.3.0-phase2`

**Commit:** `tag phase 2 complete; aider integration verified`

---

# PHASE 3 — Streaming + second provider

**Goal:** Streaming with buffer-first-chunk failover. NVIDIA NIM provider added. Background probe loop. Validate "zero `core/` changes when adding a provider."

**Phase gate:** Real failover demo — 2 OpenRouter keys exhausted mid-conversation; gateway transparently fails over to NIM; client sees no error. **AND** `git diff phase-2-gateway..HEAD -- freeride/core/` is empty (no `core/` changes during NIM addition).

---

## Feature 3.1 — Streaming with buffer-first-chunk failover

**E2E test 3.1:** Make a streaming request. Force a 429 before any byte ships. Client receives a clean stream from a different (provider, model, key). Client never sees the 429.

### Task 3.1.1 — Async stream forwarder in OpenRouter provider
- 3.1.1.1: Implement `forward_chat_stream` (replace NotImplementedError)
- 3.1.1.2: SSE → `ChatStreamEvent` async iterator

**Commit:** `implement openrouter forward_chat_stream`

### Task 3.1.2 — Buffer-first-chunk policy
- 3.1.2.1: `freeride/server/streaming.py` — `async def stream_with_failover(...)`
- 3.1.2.2: Hold first chunk until upstream confirms 200 + first SSE event
- 3.1.2.3: On error before first chunk: retry on next (provider, model, key)
- 3.1.2.4: After first chunk shipped: errors propagate to client

**Commit:** `add buffer-first-chunk streaming failover`

### Task 3.1.3 — Streaming endpoint
- 3.1.3.1: Extend `/v1/chat/completions` to dispatch stream vs non-stream
- 3.1.3.2: Set SSE headers correctly

**Commit:** `wire streaming dispatch into chat-completions endpoint`

### Task 3.1.4 — Tests
- 3.1.4.1: Mock streaming response; failover before first chunk works
- 3.1.4.2: Failover after first chunk surfaces error

**Commit:** `add streaming failover tests`

---

## Feature 3.2 — Cancellation propagation

**E2E test 3.2:** Client disconnects mid-stream. Upstream provider receives cancel within 1s (verified via mock provider's cancellation hook).

### Task 3.2.1 — Async cancellation plumbing
- 3.2.1.1: Use FastAPI's `request.is_disconnected()` polling
- 3.2.1.2: Propagate `asyncio.CancelledError` to upstream httpx call

**Commit:** `propagate client disconnect to upstream provider`

### Task 3.2.2 — Tests
- 3.2.2.1: Mock client disconnect; assert upstream cancel within timeout

**Commit:** `add cancellation propagation test`

---

## Feature 3.3 — NVIDIA NIM provider

**E2E test 3.3:** With `NVIDIA_API_KEY` set, `provider.list_free_models(key)` returns NIM's free-tier models. `provider.probe(model_id, key)` succeeds for at least one model.

### Task 3.3.1 — NIM provider scaffold
- 3.3.1.1: `freeride/providers/nvidia_nim.py` — class skeleton implementing `Provider`

**Commit:** `scaffold nvidia_nim provider`

### Task 3.3.2 — Free-detection logic
- 3.3.2.1: NIM doesn't expose `:free` suffix; rely on documented free-credit tier list
- 3.3.2.2: Hardcode known free models (publicly listed) as a starter set; revisit when NIM adds an API for it

**Commit:** `add nim free-model detection (hardcoded starter list)`

### Task 3.3.3 — list_free_models
- 3.3.3.1: Hit NIM `/v1/models`; cross-reference free-list

**Commit:** `implement nim list_free_models`

### Task 3.3.4 — probe + classify_error
- 3.3.4.1: NIM error shapes — map to `ErrorKind`
- 3.3.4.2: NIM uses HTTP 429 for rate limit; possibly HTTP 402 for quota

**Commit:** `implement nim probe and error classification`

### Task 3.3.5 — forward_chat / forward_chat_stream
- 3.3.5.1: NIM is OpenAI-compatible at `/v1/chat/completions` — pass-through

**Commit:** `implement nim forward_chat with streaming`

### Task 3.3.6 — auth + attribution
- 3.3.6.1: `Authorization: Bearer <key>`
- 3.3.6.2: `attribution_headers()` returns `{}` (NIM has no equivalent)

**Commit:** `implement nim auth headers`

### Task 3.3.7 — Register provider
- 3.3.7.1: `freeride/providers/__init__.py` — auto-register if `NVIDIA_API_KEY` is set
- 3.3.7.2: Conformance test passes for `NVIDIANIMProvider`

**Commit:** `register nim provider; conformance test passes`

### Task 3.3.8 — Tests
- 3.3.8.1: Mocked unit tests for all surfaces
- 3.3.8.2: Live test (skip if no key) hits NIM real

**Commit:** `add nim provider tests`

---

## Feature 3.4 — Background probe loop

**E2E test 3.4:** Start gateway with both providers configured. Wait 16 minutes. Verify probe loop fired ≥ 1 time per provider; no quota burned beyond top-N (e.g. ≤ 5 probes per provider per loop).

### Task 3.4.1 — Probe scheduler
- 3.4.1.1: `freeride/server/probes.py` — async background task
- 3.4.1.2: Default interval 15 min; configurable
- 3.4.1.3: Probe top-N (default 5) per provider

**Commit:** `add background probe scheduler with budget`

### Task 3.4.2 — Piggyback on real traffic
- 3.4.2.1: Track `last_seen_at` per tuple from real-request results
- 3.4.2.2: Skip probing tuples seen in last 15 min

**Commit:** `skip probes for recently-seen tuples`

### Task 3.4.3 — Lifespan integration
- 3.4.3.1: Start scheduler in FastAPI startup; cancel in shutdown

**Commit:** `wire probe scheduler into app lifespan`

### Task 3.4.4 — Tests
- 3.4.4.1: Mock provider; verify probes fire on schedule
- 3.4.4.2: Verify piggyback skip

**Commit:** `add probe scheduler tests`

---

## Phase 3 gate

### Task 3.G.1 — Cross-provider failover demo
- 3.G.1.1: Start gateway with OpenRouter + NIM keys
- 3.G.1.2: Force OpenRouter rate limit via test endpoint
- 3.G.1.3: Make streaming request; verify NIM serves it; client sees no error

**Commit:** `add cross-provider failover demo script`

### Task 3.G.2 — Validate seam quality
- 3.G.2.1: Run `git diff phase-2-gateway..HEAD -- freeride/core/`
- 3.G.2.2: Diff must be empty; if not, regroup before Phase 4

**Commit:** `tag phase 3 complete; seam quality verified`

---

# PHASE 4 — Feature passthrough + agent bind

**Goal:** Tool calls, structured outputs, vision, logprobs flow through correctly. `freeride bind <agent>` writes one URL into one config file per supported agent.

**Phase gate:** Real Continue install + `freeride bind continue` + free AI works end-to-end with cross-provider failover. No manual config tweaks.

---

## Feature 4.1 — Tool call passthrough

**E2E test 4.1:** Send a `chat/completions` request with `tools` parameter. Response includes `tool_calls`. Round-trip a second turn with `tool_call_id` and `role: tool` message; final response references the tool result.

### Task 4.1.1 — Schema completeness
- 4.1.1.1: Verify `ChatRequest`/`ChatResponse` Pydantic models include `tools`, `tool_choice`, `tool_calls`, `tool_call_id`

**Commit:** `verify tool-call fields present in chat schemas`

### Task 4.1.2 — Provider error nuance
- 4.1.2.1: New `ErrorKind` member or sub-classification: `TOOL_CALL_UNSUPPORTED`
- 4.1.2.2: Resolver skips models without `tools` in `supported_parameters`

**Commit:** `add tool-call support detection in resolver`

### Task 4.1.3 — Tests
- 4.1.3.1: Live test against a known tool-supporting model

**Commit:** `add tool-call e2e test`

---

## Feature 4.2 — Structured outputs (response_format)

**E2E test 4.2:** Send request with `response_format: {"type": "json_schema", ...}`. Response strictly conforms to the schema (validate via jsonschema).

### Task 4.2.1 — Pass-through with provider capability check
- 4.2.1.1: Resolver checks `response_format` requires `structured_outputs` in `supported_parameters`
- 4.2.1.2: Skip models that don't support; surface clean error if none do

**Commit:** `add structured-output capability check in resolver`

### Task 4.2.2 — Tests
- 4.2.2.1: Live test with json_schema; jsonschema validate response

**Commit:** `add structured-output e2e test`

---

## Feature 4.3 — Vision passthrough

**E2E test 4.3:** Send request with `image_url` content. Response describes the image.

### Task 4.3.1 — Multimodal request handling
- 4.3.1.1: `ChatRequest.messages.content` accepts `list[ContentPart]` with `text` or `image_url` types
- 4.3.1.2: Resolver checks `vision` in `supported_parameters`

**Commit:** `support multimodal content parts; resolver checks vision`

### Task 4.3.2 — Tests
- 4.3.2.1: Live test with a 1×1 transparent png; assert response is non-error

**Commit:** `add vision e2e test`

---

## Feature 4.4 — Logprobs passthrough

**E2E test 4.4:** Send request with `logprobs: true, top_logprobs: 5`. Response includes `choices[].logprobs.content` array.

### Task 4.4.1 — Pass-through
- 4.4.1.1: Already in pass-through schema; verify provider doesn't strip
- 4.4.1.2: Resolver: `logprobs` in `supported_parameters` (rarely the case for free models)

**Commit:** `verify logprobs pass-through with capability check`

### Task 4.4.2 — Tests
- 4.4.2.1: Live test (skip if no model supports it)

**Commit:** `add logprobs e2e test`

---

## Feature 4.5 — `freeride bind openclaw`

**E2E test 4.5:** Fresh OpenClaw config + `freeride bind openclaw http://localhost:11343/v1` → OpenClaw routes through gateway. Gateway logs show OpenClaw traffic.

### Task 4.5.1 — Bind helper for OpenClaw
- 4.5.1.1: `freeride/cli/cmd_bind.py` — dispatches to per-agent binders
- 4.5.1.2: `freeride/binders/openclaw.py` — sets gateway URL in `~/.openclaw/openclaw.json` auth profile

**Commit:** `add freeride bind openclaw`

### Task 4.5.2 — Preserves unrelated keys
- 4.5.2.1: Atomic write; round-trip preserves user's gateway/channels/plugins keys

**Commit:** `verify openclaw bind preserves unrelated keys`

### Task 4.5.3 — Tests
- 4.5.3.1: Fixture config; bind; assert only auth profile changed

**Commit:** `add openclaw bind tests`

---

## Feature 4.6 — `freeride bind aider`

**E2E test 4.6:** `freeride bind aider` writes `OPENAI_API_BASE` to `.aider.conf.yml` (or shell env). `aider` round-trips successfully.

### Task 4.6.1 — Aider binder
- 4.6.1.1: `freeride/binders/aider.py` — writes `~/.aider.conf.yml` with `openai-api-base: http://localhost:11343/v1`

**Commit:** `add freeride bind aider`

### Task 4.6.2 — Tests
- 4.6.2.1: Fixture aider config; bind; assert correct key written

**Commit:** `add aider bind tests`

---

## Feature 4.7 — `freeride bind continue` (phase gate)

**E2E test 4.7 (= phase gate):** Real Continue install + default config + `freeride bind continue` + restart Continue → free AI request via Continue UI lands in gateway logs and responds.

### Task 4.7.1 — Continue binder
- 4.7.1.1: `freeride/binders/continue_.py` — writes one block to `~/.continue/config.json`
- 4.7.1.2: Block: `{"models": [{"title": "freeride", "provider": "openai", "apiBase": "http://localhost:11343/v1", ...}]}`

**Commit:** `add freeride bind continue`

### Task 4.7.2 — Tests
- 4.7.2.1: Fixture continue config; bind; assert preserves unrelated keys

**Commit:** `add continue bind tests`

### Task 4.7.3 — Phase 4 gate
- 4.7.3.1: Live test in Daytona with real Continue install
- 4.7.3.2: Tag `v0.3.0-phase4`

**Commit:** `tag phase 4 complete; continue integration verified`

---

# PHASE 5 — Telemetry, docs, ship

**Goal:** Telemetry beacon implemented per §14. Public README. Install script. PyPI release.

**Phase gate:** `pip install freeride` from PyPI works on a fresh machine; install script writes a working setup; telemetry off-by-default verified.

---

## Feature 5.1 — Telemetry beacon

**E2E test 5.1:** `freeride telemetry off` → no network traffic to beacon endpoint after 1 hour. `freeride telemetry on` → exactly one beacon POST per hour with the documented payload shape.

### Task 5.1.1 — Installation ID
- 5.1.1.1: `freeride/core/telemetry.py` — `installation_id()` generates UUID4 to `~/.freeride/installation_id` on first run

**Commit:** `add installation_id helper`

### Task 5.1.2 — Beacon scheduler
- 5.1.2.1: Hourly tick; reads stats; POSTs to `https://telemetry.freeride.dev/v1/beacon` (URL TBD)
- 5.1.2.2: Silent failure mode; never blocks

**Commit:** `add hourly telemetry beacon scheduler`

### Task 5.1.3 — Opt-in gate
- 5.1.3.1: Skip entirely if `~/.freeride/config.json` lacks `telemetry: true`
- 5.1.3.2: Default config: telemetry off

**Commit:** `gate telemetry behind explicit opt-in`

### Task 5.1.4 — Payload spec
- 5.1.4.1: Payload exactly matches §14 of PLAN_GATEWAY.md
- 5.1.4.2: NEVER include prompts, completions, model IDs, keys

**Commit:** `lock down telemetry payload to spec`

### Task 5.1.5 — Tests
- 5.1.5.1: Default-off behavior; opt-in respected; payload shape

**Commit:** `add telemetry tests`

---

## Feature 5.2 — `freeride telemetry` CLI

**E2E test 5.2:** `freeride telemetry` shows current state + verbatim payload preview before any decision.

### Task 5.2.1 — Command
- 5.2.1.1: `freeride/cli/cmd_telemetry.py` — `on`, `off`, no-arg status
- 5.2.1.2: No-arg: prints state + sample payload (real values from current stats)

**Commit:** `add freeride telemetry on/off/status`

### Task 5.2.2 — Tests
- 5.2.2.1: State transitions; payload preview

**Commit:** `add telemetry cli tests`

---

## Feature 5.3 — Public README

**E2E test 5.3:** Project root `README.md` covers: install, get OpenRouter key, `freeride serve`, `freeride bind <agent>`, status, troubleshooting. Markdown lint clean.

### Task 5.3.1 — Draft README
- 5.3.1.1: Replace v2 README with V3 narrative
- 5.3.1.2: Section: "What FreeRide is" (gateway, free, local-first)
- 5.3.1.3: Section: "Quick start" (install + serve + bind)
- 5.3.1.4: Section: "Telemetry" (transparent disclosure)

**Commit:** `add v3 README`

### Task 5.3.2 — Examples directory
- 5.3.2.1: `examples/` with shell scripts: aider, continue, raw curl

**Commit:** `add examples directory`

---

## Feature 5.4 — Install script

**E2E test 5.4:** `curl -fsSL <install-url> | sh` on a fresh macOS Daytona box → `freeride --version` works.

### Task 5.4.1 — Install script
- 5.4.1.1: `scripts/install.sh` — checks Python ≥ 3.10, pip, then `pip install freeride`
- 5.4.1.2: Prints first-run guidance

**Commit:** `add install script`

---

## Feature 5.5 — PyPI release

**E2E test 5.5:** `pip install freeride==0.3.0` from a fresh Daytona box, on a fresh venv, succeeds. `freeride serve` runs.

### Task 5.5.1 — Build
- 5.5.1.1: `python -m build` produces wheel + sdist

**Commit:** `verify build artifacts`

### Task 5.5.2 — TestPyPI dry run
- 5.5.2.1: `twine upload --repository testpypi dist/*`
- 5.5.2.2: Install from TestPyPI in fresh venv

**Commit:** `verify TestPyPI install path`

### Task 5.5.3 — Production release
- 5.5.3.1: `twine upload dist/*` to PyPI
- 5.5.3.2: Verify install on fresh Daytona box

**Commit:** `release v0.3.0 to PyPI`

### Task 5.5.4 — Tag
- 5.5.4.1: `git tag v0.3.0` and push tags

**Commit:** `tag v0.3.0 release`

---

# Items requiring user input (NOT autonomous)

These are explicitly outside the execution loop — I will pause and ask:

- **Daytona credentials and access** — required to start Phase 1.
- **OpenRouter API key (≥ 1, ideally 3 for multi-key tests)** — required to start Phase 1 Feature 1.4 onward.
- **NVIDIA NIM API key** — required to start Phase 3 Feature 3.3.
- **Telemetry endpoint URL + infra** — required for Phase 5 Feature 5.1 production rollout. (Stub locally fine for dev.)
- **PyPI API token** — required for Phase 5 Feature 5.5.
- **Decision: keep `FreeRide` name vs rename** — D10 says keep; confirm before tagging v0.3.0.
- **Decision: replace v2 vs two products** (PLAN_GATEWAY.md §9 Phase 5) — required at Phase 5 Feature 5.3 (README narrative depends on it).

---

# Out-of-scope for this execution plan

These appear in `PLAN_GATEWAY.md` as deferred and will not be executed under the $100 budget:

- **Phase 6 Feature 6.1** — Anthropic Messages API surface
- **Phase 6 Feature 6.2** — Embeddings endpoint
- **Self-hosted multi-tenant gateway** — explicit non-goal

---

# Daytona budget tracking

- **Hard cap:** $100. Halt at $80 and report.
- **Soft alert:** every Phase gate, report cumulative spend.
- **Cost drivers:** dev environment uptime, optional GPU runners (none expected — this is CPU-only Python).
- **Optimization:** stop the Daytona workspace when paused for user input. Never leave it idle running for >2h with no commits.

---

# Estimated commit count

Rough commit budget across phases (one per task):

| Phase | Tasks | Commits |
|---|---|---|
| 1 | ~25 | ~25 |
| 2 | ~20 | ~20 |
| 3 | ~14 | ~14 |
| 4 | ~13 | ~13 |
| 5 | ~14 | ~14 |
| **Total** | **~86** | **~86** |

Plus phase tags (5). Plus E2E test additions per feature (already counted as tasks). Net commit log readable as a step-by-step build history.

---

# Done definition for V3

V3 is "done" when ALL of:
1. `pip install freeride` from PyPI works.
2. `freeride bind <openclaw|aider|continue>` produces a working free-AI setup.
3. Cross-provider failover (OpenRouter ↔ NIM) verified end-to-end.
4. Telemetry off-by-default verified; payload spec frozen.
5. README + examples land on `main`.
6. All phase gates passed; tags `v0.3.0-phase{1..5}` exist.
7. Cumulative Daytona spend ≤ $100.

Anything beyond this is Phase 6+, deferred.
