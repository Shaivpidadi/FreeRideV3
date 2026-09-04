# Changelog

All notable changes to FreeRide are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

## [0.4.0a23] — 2026-09-04

Hardening on the fx/agent path from live use and code review.

### Added
- The fallback ladder learns from live failures: a failed candidate
  writes a short-TTL ``recent_failure`` mark into the model-health
  cache, so consecutive turns stop re-paying pre-flight time on a cold
  (provider, model) pair; a recently-failed agent pin demotes to the
  end of the ladder instead of being dropped.
- Local per-(provider, model) ok/fail counters under ``model_usage``
  in ``~/.freeride/stats.json``. Local-only — the telemetry beacon
  payload is unchanged.
- The Cloudflare worker serves the ridex installer at
  ``GET /ridex.sh``.

### Fixed
- fx streaming review findings: exactly one ``response-metadata``
  frame per turn (a mid-stream candidate switch previously emitted a
  second one, which strict SSE consumers can reject); client
  disconnect during pre-flight no longer orphans the shielded failover
  task; ``/health`` builds one ``KeyCooldown`` for the whole check
  instead of one per provider.
- ``sync_dbs`` drops its stale temp table before recreate (Neon
  pooler connection reuse).

## [0.4.0a22] — 2026-09-02

The fx gateway dialect: FreeRide now natively serves ridex, our fork
of vercel-labs/fx, making it a first-class coding-agent backend.

### Added
- `POST /v3/ai/language-model` + `GET /coding-agent/v1/models` —
  the Vercel AI SDK language-model wire dialect fx's stock transport
  speaks. Requests translate to Chat Completions; responses stream
  back as AI SDK parts (`text-delta`, `tool-input-*`, `tool-call`,
  `finish` with the unified-enum reason object). Model id rides the
  `ai-language-model-id` header (`freeride/core/fx_translate.py`,
  `fx_schema.py`, `server/routes/fx.py`).
- **Universal fallback ladder**: every fx request carries an ordered
  ladder of (provider, tools-capable model) candidates — the coding
  pin or the concretely requested model first, then per-provider
  fallbacks filtered by the `tools` capability and the model-health
  cache. A provider with no free inference right now (rate limit,
  dead key, retired model) is absorbed silently inside the same
  response.
- `/health` reports `keyed_providers` — providers that actually hold
  a usable, non-cooling key (registration alone doesn't imply keys).

### Fixed
- fx streaming ships the 200 + headers immediately and holds the line
  with `: preflight` SSE keepalive comments while failover waits for
  the upstream's first token — free-tier TTFT no longer trips the
  agent's first-byte timeout (previously 1–2 visible retries per
  turn).
- Mid-stream upstream death is reported honestly: before any output
  the walk switches candidates silently; after output the turn ends
  with an in-stream `error` part + `finish` `unified: "error"` so
  the agent retries — no more fabricated clean finishes on truncated
  answers.
- `freeride serve`'s port probe binds with `SO_REUSEADDR`, so
  stop-then-start no longer false-positives "already in use" on
  TIME_WAIT sockets.

## [0.4.0a21] — 2026-08-21

Fixes from the architecture review: cooldown no longer stores raw API
keys, TTL now matches the documented per-kind policy, and the failover
walk lives in one module instead of five copies.

### Added
- `freeride/core/failover.py` — shared `try_call_with_failover` /
  `try_stream_with_failover` used by chat, messages, responses, gemini,
  and embeddings.
- `freeride/core/provider_env.py` — single provider ↔ env-var registry.
  Numbered suffixes (`OPENROUTER_API_KEY_2`) actually work now.
- Windows unit-test job (`windows-latest`, Python 3.12).

### Fixed
- Cooldown JSON keys are SHA-256 prefixes, not the raw secret. Legacy
  files migrate on first read.
- Cooldown TTL is per error kind: Retry-After (else 60s) for rate
  limit, 5 min for auth, 60 min for quota. Previously everything was
  a flat 120s and quota keys got retried immediately.
- Docs/comments that still talked about Phase-2 stubs, a missing
  `docs/agent-binders.md` link, and a cooldown table that didn't match
  the code.
- `freeride keys` now loads `~/.freeride/.env` (same as `doctor` /
  `serve`) instead of only process env.
- Groq free allowlist updated for current catalog ids (`openai/gpt-oss-20b`,
  `qwen/qwen3.6-27b`, …). Retired Llama ids no longer match Groq's API.

### Changed
- `KeyCooldown.mark(provider, key, kind, retry_after_s=...)` is the
  new mutation. `mark_rate_limited` remains as a RATE_LIMIT wrapper.
- NVIDIA NIM also accepts `NIM_API_KEY` as an alias of `NVIDIA_API_KEY`.
- Pin ruff to the pre-0.16 E/F default set (0.16 enabled 413 rules and
  broke CI). Windows pytest uses `USERPROFILE` for `Path.home()`.


## [0.4.0a20] — 2026-05-29

Hotfix for `freeride run codex`. Codex 0.121+ defaults to a WebSocket
transport on `/v1/responses` and only falls back to HTTP after five
failed reconnect attempts — which the gateway can't satisfy (it
speaks HTTP/SSE, not WS). The end-to-end answer still came through
but stderr was full of `failed to connect to websocket: 403 Forbidden`
lines that looked like a hard failure.

### Fixed
- `freeride run codex` no longer logs WebSocket reconnect errors.
  `prepare_codex_argv` now injects a full custom `model_providers.freeride`
  block via `-c` flags instead of the old `openai_base_url` shortcut,
  and sets `supports_websockets=false` on that provider so codex skips
  the upgrade attempt entirely. Reaching the gateway is just one
  clean HTTP request per turn.

## [0.4.0a19] — 2026-05-29

Telemetry pipeline lands its first real numbers. Two bugs that
combined to make the beacon counter useless are fixed end-to-end:
the streaming routes were passing `tokens=0` (so every modern agent
CLI request — claude, codex, gemini, Cursor, Cline, Aider, all of
which stream by default — went uncounted), and `/v1/stats` was
summing cumulative per-row snapshots from every beacon, inflating
whatever it did see by ~150×. Storage is also moved off Cloudflare
D1 onto Neon Postgres so the marketing site, the gateway, and any
future SDK callers all read from one place.

### Added
- `freeride/core/usage.py` — provider-format-aware extractor that
  parses `usage.prompt_tokens` / `usage.input_tokens` /
  `usageMetadata.promptTokenCount` etc. into a single
  `Usage(input, output)` shape. Works on full response objects and
  on individual SSE chunks. Handles Anthropic prompt-cache fields
  (`cache_creation_input_tokens`, `cache_read_input_tokens`).
- `record_request(input_tokens=..., output_tokens=..., provider=...)`
  signature for separate prompt vs. completion accounting. Legacy
  `tokens=` keyword still accepted so older route callers keep
  compiling during the migration.
- Beacon payload now carries `input_tokens` and `output_tokens`
  alongside the legacy `tokens_served` sum.
- `_force_stream_usage(body)` helper in `chat.py` injects
  `stream_options.include_usage=true` on every outgoing OpenAI-compat
  streaming request, so providers actually ship the usage chunk.

### Fixed
- **Streaming routes count tokens.** Chat (`/v1/chat/completions`),
  Messages (`/v1/messages`), Codex (`/v1/responses`), and Gemini
  (`/v1beta/models/...`) all hold a `last_usage` box, swap it on
  every event that carries a usage block, and record the final
  value on stream completion. Previously every streaming response
  recorded `tokens=0`.
- **`/v1/stats` math.** Rewritten to use
  `DISTINCT ON (installation_id) ... ORDER BY received_at DESC` and
  sum across the latest beacon per install. Reported totals drop
  from a ~355M over-count back to the actual ~2.4M.
- **`/v1/stats` cache.** `Cache-Control: no-store` set on the JSON
  response so Cloudflare's edge stops serving stale aggregates
  after a deploy.

### Changed
- Telemetry receiver Worker (`api.free-ride.xyz`,
  `telemetry.free-ride.xyz`) moved from Cloudflare D1 to Neon
  Postgres. Schema `services/telemetry/schema.pg.sql` is the new
  source of truth; all 5,690 rows from D1 migrated by the one-off
  `services/telemetry/migrate_d1_to_neon.py`. D1 binding removed
  from `wrangler.toml`; the connection string is a Worker secret
  (`wrangler secret put DATABASE_URL`) plus a gitignored
  `.dev.vars` for local `wrangler dev`. Old D1 database kept as a
  backup for a week.

## [0.4.0a4] — 2026-05-10

Adoption-tracking release. Closes the install-vs-token-served blind spot:
the existing hourly beacon only sees CLIs that run `freeride serve` for
>1h with telemetry on, so most one-shot or short-session users never
report. We now fire a one-shot install event at install time and a
startup beacon shortly after `serve` boots, so brief sessions are
captured too.

### Added
- **One-shot install event from `install.sh` / `install.ps1`.** Runs
  after the `freeride` binary is on PATH but before the user has
  invoked anything; POSTs `{installation_id, version, os,
  install_method}` to a new `POST /v1/install-event` endpoint on
  api.free-ride.xyz, which writes one row to a new
  `install_events` D1 table (PK `installation_id`, INSERT OR
  IGNORE). Uses `~/.freeride/installation_id` as the source of
  truth — same UUID the gateway later persists at runtime, so
  install events and beacons share an id and can be correlated.
  Best-effort; a failure of the POST never breaks the install.
  Skipped when `--no-telemetry` is passed or `FREERIDE_TELEMETRY=off`.
- **Startup beacon.** `freeride serve` now fires `ship_beacon()` once
  about 30 seconds after lifespan startup, before entering the
  hourly steady-state loop. Captures users who run the gateway for
  less than an hour — previously invisible to adoption telemetry.
- **`freeride doctor` surfaces telemetry status.** New row showing
  on/off, install_id (truncated to 8 chars for screenshot privacy),
  beacon endpoint, and the audit command. Honest disclosure of what
  telemetry is sending.
- **Install velocity in `/v1/stats`.** New `installs` block reports
  `total`, `last_24h`, `last_7d`, `last_30d` from the
  install_events table — independent of whether those CLIs ever
  reached the beacon path. The marketing leaderboard and BD
  pitches can now show "real installs" alongside "tokens served".

### Internal
- `services/telemetry/schema.sql`: new `install_events` table +
  `idx_install_events_installed_at` index.
- `services/telemetry/src/worker.js`: `handleInstallEvent`,
  `/v1/install-event` route, `installs` block in `handleStats`,
  embedded `INSTALL_SH` + `INSTALL_PS1` strings updated to match
  the repo-root copies.

## [0.4.0a3] — 2026-05-09

Smart-routing release. The `model: "auto"` keyword now resolves to a
concrete model picked by score, not just the first catalog entry that
matches a usable provider — and a new persistent runtime health cache
populated by `freeride audit-models` keeps known-broken / quota-
exhausted / rate-limited models out of the resolution pool until they
recover. Plus per-day per-model OR token attribution surfaced in
`/v1/stats`, a clean SVG chart on the public `/models` leaderboard,
and a confirmed-ghost prune on the Cerebras provider.

### Added
- **`model: "auto"` keyword in `/v1/chat/completions`.** Sentinel
  set: `auto`, `default`, `freeride/auto`, `""`, `null`. Resolves
  against the live `/v1/models` catalog before dispatch; emits an
  `auto_model_resolved` event for `freeride watch`. Returns 503
  `no_model_available` with a `freeride list` / `freeride keys`
  suggestion when no resolvable model + key combination exists.
- **Smart-routing scorer.** New `freeride/core/smart_routing.py`
  weights each catalog entry by 10 × failover-headroom + log10(global
  popularity + 1) × 5. Popularity sourced from the public
  `api.free-ride.xyz/v1/stats` endpoint with on-disk cache (1h TTL,
  graceful empty-fallback on network/SSL failure).
- **`freeride audit-models` subcommand.** Probes every model in every
  configured provider's catalog and persists a per-(provider, model)
  health verdict to `~/.freeride/cache/model_health.json` (24h TTL).
  `--workers N` for concurrency, `--provider X` to restrict, `--quiet`
  for summary-only output. Smart-routing reads this cache and
  zero-scores models flagged broken — auto-resolution skips them
  entirely.
- **Reactive catalog cache invalidation.** Both streaming and
  non-streaming chat paths now call `invalidate_catalog()` when a
  provider returns `MODEL_NOT_FOUND`, so the next `/v1/models` or
  auto-resolve call rebuilds against current upstream catalogs.

### Fixed
- **Cerebras catalog drops two confirmed ghost ids.** `zai-glm-4.7`
  and `gpt-oss-120b` are advertised by Cerebras's `/models` endpoint
  but every chat completion against them returns `model_not_found`.
  Filtered at `list_free_models()` time so the smart-routing resolver
  and `/v1/models` response no longer hand out an id that can't
  actually serve. Refresh the list by re-running
  `freeride audit-models --provider cerebras`.

### Telemetry / observability
- **Per-day per-model OR breakdown.** The Cloudflare telemetry worker
  (under `services/telemetry/`) now scrapes the OR app activity
  page's daily series and upserts one row per (date, app, model_id)
  into a new `openrouter_daily` D1 table. `/v1/stats` exposes
  `openrouter_daily.last_7d` (date / tokens / models_count per day)
  and `openrouter_daily.top_models_30d` (top 10 by tokens).
- **`auto_model_resolved` event.** Emitted to
  `~/.freeride/events.jsonl` for every `model: "auto"` request that
  successfully resolves; carries `resolved_model` and
  `resolved_provider`. Visible via `freeride watch`.

## [0.4.0a2] — 2026-05-08

Operator-experience release. New diagnostic CLIs (`doctor`, `keys`,
`providers`, `bench`, `init`, `upgrade`, `reload`), security tightening
(0o600 mode for on-disk secrets), per-key health tracking, third-party
provider plugin discovery via Python entry points, and a 7th provider
(Cerebras). Real-network e2e validated against 5 providers
(OpenRouter, Groq, NVIDIA NIM, HuggingFace, Cerebras) — 21 tests
passing, 0 failures. Two real bugs caught and fixed by the e2e run:
HuggingFace router's missing `/embeddings` endpoint and NVIDIA NIM's
required `input_type` field.

### Changed
- **`freeride serve` and `freeride doctor` auto-load `~/.freeride/.env`**
  at startup. The `freeride init && freeride serve` flow now works
  without a manual `source` step. `build_provider_registry()` re-loads
  the dotenv on every call, so `freeride reload` after editing the
  file picks up newly-added provider keys. OS env vars always win
  over `.env` — explicit shell exports remain stronger than a stale
  file. Pure-python parser at `freeride/core/dotenv.py` (no new pip
  dep). 16 unit tests covering the parser, env-merge contract, OS-env-wins
  guarantee, malformed-file silent-fallback, and integration with
  `build_provider_registry()`.
- **`freeride bench` probes providers in parallel by default.** With
  7 providers × 3 requests × ~500ms each, sequential bench took ~10s
  wall clock. Parallel cuts to ~max(provider_times) ≈ 1.5s. Pass
  `--sequential` for the old behavior — useful for clean per-provider
  latency measurements without local-resource contention from
  concurrent requests. Uses `concurrent.futures.ThreadPoolExecutor`;
  no event loop needed since httpx is sync inside the bench runner.

### Security
- **All on-disk secrets now mode 0o600 (owner read/write only).**
  `core.state.atomic_write()` now chmods every file it writes to
  `0o600` by default. Files that contain or could contain provider
  API keys — `~/.freeride/cooldown.json` (keys live as JSON object
  keys), `~/.freeride/.env` written by `freeride init`, any future
  state file routed through `atomic_write` — automatically inherit
  this. Pre-existing world-readable files get tightened on the next
  write. Windows / non-POSIX FS chmod is best-effort and logged.

### Added
- **Third-party provider plugin discovery** via Python entry points.
  Distributors of FreeRide-compatible provider plugins ship them as
  separate pip packages and register the class under the
  `freeride.providers` entry-point group. At `freeride serve` startup,
  `discover_third_party_providers()` iterates the entry points, loads
  each class, and instantiates it. Construction failures (missing env
  vars, etc.) are logged and skipped — one bad plugin can't prevent
  the gateway from starting. Plugins on a different `api_version` are
  skipped with a version-mismatch warning. 7 hermetic tests covering
  the load / construction-fail / api-version-mismatch paths.
- **Cerebras provider** (`freeride/providers/cerebras.py`).
  OpenAI-compatible at `api.cerebras.ai/v1`. Adds a 7th free-tier
  provider — Cerebras has the fastest Llama / Qwen inference of any
  remote free tier (~1k tokens/sec on llama3.1-8b). Chat-only, no
  embeddings (`embeddings_supported = False` so the embeddings route
  filter skips it). Classifier handles OpenAI-shape error envelope:
  `code: model_not_found` → MODEL_NOT_FOUND, "quota exceeded" message
  → QUOTA_EXHAUSTED. Auto-loaded by `freeride serve` when
  `CEREBRAS_API_KEY` is set. `CEREBRAS_FREE_MODELS_OVERRIDE` env var
  restricts the catalog if you have a paid plan with restricted access.
  Wired into `freeride init`, `freeride doctor`, `freeride keys`,
  e2e matrix, conformance suite. 14 unit tests.
- **Per-key health tracking and ordering.** The health module now also
  tracks rolling success-rate + p50 latency per `(provider, key_hash)`
  in addition to the per-provider rollup. The chat and embeddings
  routes call `sort_keys_by_health(provider, keys)` to sort within a
  provider's keys before walking them, so a single flaky key gets
  demoted relative to its siblings without affecting the provider's
  overall ordering. Privacy: keys are stored hashed (SHA256 prefix)
  inside the tracker; raw key never persists. Backward-compatible:
  `record()` still works without a `key=` arg (continues to update
  only the provider rollup), so non-route callers don't need changes.
- **`freeride keys` CLI** — show which provider keys are available
  vs cooling. Reads `~/.freeride/cooldown.json` directly (no need for
  the gateway to be running) and cross-references with the per-provider
  env vars currently set. Privacy-conscious: actual key values never
  printed, just `k0`/`k1` indices plus a stable 8-char hash. Per-row:
  total keys, available count, cooling count, "soonest back" countdown.
  `--verbose` adds a per-key breakdown showing exactly which key is
  cooling and how long.
- **`freeride init` CLI** — interactive setup wizard. Walks through
  every supported provider, shows the signup URL (optional
  `--open-browser` opens each one in the user's default browser),
  prompts for the env var(s), writes a `~/.freeride/.env` file
  (or `--out <path>`). Re-running is non-destructive — only
  user-entered values overwrite, existing keys preserved. Empty
  input skips a provider. Ctrl-C aborts without writing. Cuts
  onboarding from "read README + sign up + paste 7 exports" to one
  guided command. After writing, prints next-steps:
  `source ~/.freeride/.env && freeride serve`.
- **`freeride providers` CLI** — pretty-printed live health from a
  running gateway. Hits `/v1/_freeride/providers` and renders a table:
  per-provider attempt count, success rate, p50 latency, computed
  health score, embeddings-supported flag. Cold rows (below the
  health min-N threshold) are dimmed and tagged `(cold)`. Footer
  picks the healthiest warm provider as the operational answer to
  "who's actually serving requests well right now?".
- **`freeride doctor` CLI** — one-command diagnostics for the most-asked
  "why isn't this working?" cases. Walks Python version, `freeride`
  on PATH, `~/.freeride/` writability, every provider env var (with
  partial-config warnings for CF and either-or HF), and either
  `port 11343 free` or `gateway already running on it`. Returns 1
  on hard errors, 0 on warnings or all-green. Color-coded glyphs
  (✓ / ! / ✗ / ·).
- **`freeride upgrade` CLI** — bump the installed package to the latest
  PyPI release. Detects how FreeRide was installed (uv tool /
  pipx / pip) and runs the right upgrade command, then re-imports in a
  subprocess to confirm the new version. Exits non-zero if the upgrade
  subprocess fails. `--dry-run` prints what would run without
  executing. Friendly nudge: "restart `freeride serve` to pick up the
  new version" if a gateway is running. The `uv` strategy ships with
  `--prerelease=allow` so 0.x alphas keep flowing through.
- **Hot-reload of provider registry** without restart.
  `POST /v1/_freeride/reload` rebuilds `app.state.providers` from the
  current env vars by re-running the build-registry factory.
  `freeride reload` CLI POSTs to that endpoint and prints
  before/after/added/removed. Atomic swap: in-flight requests already
  holding their own snapshot of the provider list aren't affected.
  Useful for the common "I forgot to set GROQ_API_KEY before starting
  serve" case. Returns `reload_not_enabled` (ok=false) when the server
  was started without a `provider_factory` (test apps pin a fixed list).
- **Ollama provider** (`freeride/providers/ollama.py`). Local Ollama
  daemon as a first-class FreeRide provider — same Provider Protocol,
  same failover chain, same `freeride watch` integration. Lets users
  mix local models with remote free tiers (e.g., "try local Llama
  3.1 first, fall back to OpenRouter if it's not loaded"). Opt in
  via `OLLAMA_BASE_URL` (default `http://localhost:11434`).
  No auth — Ollama is local. Embeddings supported (Ollama 0.1.40+).
  Classifier maps `ConnectError` to UNAVAILABLE so the chain
  advances cleanly when Ollama isn't running. The env var doubles as
  the chain "key" — JSON-array form lets one gateway target multiple
  Ollama hosts. Full conformance + 8 unit tests.
- **`freeride bench` CLI** — per-provider latency comparison.
  Uses `X-FreeRide-Force-Provider` to hit each registered provider
  with N tiny chat completions (default 3), times each, prints a
  sorted-by-p50 table with success rate + p50/p95 latency + tokens/s.
  Useful for "which provider is fastest right now?" — a single
  command answer instead of inspecting `freeride watch` over many
  requests. Requires `freeride serve` running.

## [0.4.0a1] — 2026-05-07

First release of the 0.4 line. Bigger surface area than 0.3.0a* —
new top-level endpoint (`/v1/embeddings`), new admin endpoint
(`/v1/_freeride/providers`), new request header (`X-FreeRide-Force-Provider`),
new response header (`X-FreeRide-Request-ID`), new CLI (`freeride watch`),
canonical-id grouping in `/v1/models`. All backward-compatible
with 0.3.0a* clients on the chat path.

### Added
- **`X-FreeRide-Force-Provider` request header.** Pins a single
  request to a specific provider, bypassing failover. Useful for
  benchmarking ("what's Groq's actual latency?"), debugging ("is OR
  really the issue?"), and any time you want to override the
  health-aware ordering. Returns 400 with `force_provider_unknown`
  when the named provider isn't registered. Works on both
  `/v1/chat/completions` and `/v1/embeddings`.
- **`GET /v1/_freeride/providers`** — diagnostic endpoint that returns
  the in-process health snapshot (per-provider attempt count, success
  rate, p50 latency, computed score, embeddings_supported flag).
  Read-only; no auth (gateway is localhost-only by design). Useful
  for surfacing live provider health into a future
  `freeride status --remote` CLI command.
- **Health-aware provider ordering** (`freeride/core/health.py`).
  Per-provider rolling-window stats (default 50 attempts) of success
  rate and p50 latency. The chat and embeddings routes sort the
  failover chain by health score so a provider that's been timing out
  recently gets demoted automatically. Sort is stable: tied providers
  (which is everything until min-N data accumulates) keep their
  registration order. Tunable: `FREERIDE_HEALTH_WINDOW` (window size),
  `FREERIDE_HEALTH_MIN_N` (min attempts before health affects order),
  `FREERIDE_HEALTH_OFF=1` to disable. New providers get a neutral 100.0
  score until they cross the min-N threshold so brand-new plugins
  aren't penalized for having no data.
- **Canonical model name normalization** (`freeride/core/canonicalize.py`).
  Strips vendor prefixes (`@cf/`, `meta-llama/`, `Qwen/`, etc.),
  quantization suffixes (`-fp8`, `-fp16`, `-q4`, `-fp8-fast`, etc.),
  Groq release-train aliases (`-instant`, `-versatile`, `-tool-use-preview`
  → `-instruct`), and HF routing-policy suffixes (`:fastest`, `:cheapest`,
  `:<provider>`). Idempotent. 28 unit tests covering each provider's
  representations of Llama 3.1 8B converging to the same key.
- **`/v1/models` grouped mode (default).** When the same logical model
  is exposed by multiple providers under different ids, returns ONE
  entry with `canonical_id`, `aliases` (list of provider-specific ids),
  and `available_providers` (list of provider names). The first-seen
  provider's id surfaces as the primary `id`. Pass `?group=false` to
  return the un-merged matrix (one entry per provider/model pair).
  All entries get `canonical_id` regardless of mode.
- **`POST /v1/embeddings` endpoint** — OpenAI-compatible embeddings
  with cross-provider failover. Implemented on OpenRouter, NVIDIA NIM,
  Cloudflare Workers AI, and HuggingFace. Groq is excluded (chat-only
  provider, no embedding endpoint). Providers opt in via
  `embeddings_supported = True` class attr; the route filters to
  capable providers before walking the failover chain. Same event-emit
  + structured-503 contract as `/v1/chat/completions`. Conformance
  suite enforces that any provider declaring support also implements
  `forward_embeddings` as `async def`. Added 6 hermetic route tests
  + 1 e2e test slot per embedding-capable provider in the matrix.
- **Per-provider e2e test matrix** (`tests/e2e/test_providers.py`).
  Each of the 5 providers gets its own subprocess gateway with ONLY
  that provider's env var(s) set, then exercises `GET /v1/models`,
  non-streaming chat completion, streaming chat completion, and
  request-id header presence. Skips cleanly when keys aren't set.
  CI runs them in a separate `e2e` job that's gated on the maintainer
  repo so forks don't try to use missing secrets.
- **Windows installer (`install.ps1`).** PowerShell mirror of
  `install.sh` — same `uv tool install` flow, drops `freeride.exe`
  in `%USERPROFILE%\.local\bin`. Served at
  `https://api.free-ride.xyz/install.ps1`. README install section
  now has both POSIX and PowerShell one-liners.
- **`freeride watch` — live failover stream.** Tails a JSONL event log
  written by the gateway (`~/.freeride/events.jsonl`) and pretty-prints
  every request, provider attempt, response, and completion in real
  time. Color-coded by status (green OK, yellow rate-limit/quota, red
  auth/unknown). Tails through file rotation. Opt out with
  `FREERIDE_EVENTS=0`. Useful for demoing failover, debugging "is my
  agent actually using FreeRide", and post-hoc inspection.
- **Structured 503 responses.** When all (provider, key) pairs fail,
  the response body is now JSON with a per-provider `tried` array
  (`provider`, `keys_tried`, `last_error`, `retry_after_s`) plus a
  `request_id` and an actionable `suggestion` string. Replaces the
  previous "All providers/keys failed; last error kind: AUTH" string.
- **`X-FreeRide-Request-ID` response header** on every chat completion
  (success and failure). Same value lands in `_freeride_request_id`
  on JSON responses and pairs each failure mode with the entries in
  `freeride watch` output.

### Changed
- **`/v1/chat/completions` failover loop** rewritten around a
  `FailoverContext` so per-provider attempt summaries can be
  collected and emitted as events without polluting the happy path.
  Behavior unchanged from a client perspective except for the new
  503 shape and headers.

## [0.3.0a8] — 2026-05-07

### Added
- **OSS hygiene files** for the public launch: `SECURITY.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `.github/ISSUE_TEMPLATE/{bug_report,feature_request,config}.{md,yml}`, `.github/PULL_REQUEST_TEMPLATE.md`.

### Changed
- **`pyproject.toml` metadata** filled out for PyPI: expanded keywords for all five providers, added `Issues`/`Documentation`/`Changelog` URLs alongside `Homepage` (now points at `free-ride.xyz`), expanded classifiers (Intended Audience, Topic, OS-specific, FastAPI, Environment), bumped status to `4 - Beta`, dropped redundant License classifier (SPDX `license` field is canonical).
- **CI lint job promoted to hard-fail.** Cleaned up 23 ruff issues (mostly unused-imports) and removed `--exit-zero` from `.github/workflows/test.yml`.
- **README hero ASCII demo** softened — was overclaiming the providers list a fresh install would surface.
- **Test fixture key** renamed `sk-real-key` → `sk-test-fixture` to avoid tripping secret scanners.

### Internal docs
- Stripped dangling references to `PLAN_GATEWAY.md` / `EXECUTION_PLAN.md` / `RELEASE_CHECKLIST.md` from source docstrings and public docs. Those files live in a private tracking repo, not the OSS source tree.

### Operational
- `services/telemetry/wrangler.toml` ships with a `REPLACE_WITH_YOUR_D1_DATABASE_ID` placeholder so forks don't accidentally bind to the upstream telemetry DB.

## [0.3.0a7] — 2026-05-07

### Added
- **Cloudflare Workers AI provider** (`freeride/providers/cloudflare_wai.py`).
  OpenAI-compatible at `api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/`.
  Account ID is part of the URL (not the key) — provider reads
  `CLOUDFLARE_ACCOUNT_ID` env var at construction time and fails loudly if
  missing. Curated free-tier allowlist of cheap-per-neuron chat models in
  `cloudflare_wai_model_metadata.py` (IBM Granite, Llama 3.x, Gemma 3,
  Qwen 2.5 Coder, Mistral Small 3.1); `CF_WAI_FREE_MODELS_OVERRIDE` env
  var lets paid-plan users expand it. Classifier handles CF's `success: false`
  envelope shape and maps 403 to AUTH (CF returns 403 when the token
  doesn't have AI permission). Auto-loaded by `freeride serve` when both
  `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set.
- **HuggingFace Inference Providers plugin** (`freeride/providers/huggingface.py`).
  OpenAI-compatible at `router.huggingface.co/v1`. HF's free tier is a
  monthly credit budget ($0.10 Free / $2 PRO), not per-model — full catalog
  passes through; classifier maps 402 Payment Required to QUOTA_EXHAUSTED
  so the resolver advances to a different provider. Optional
  `X-HF-Bill-To` org-billing header opt-in via `HUGGINGFACE_BILL_TO`
  env var. Routing-policy suffixes (`:fastest`, `:cheapest`, `:<provider>`)
  on model ids round-trip transparently. Auto-loaded by `freeride serve`
  when `HF_TOKEN` (or `HUGGINGFACE_API_KEY`) is set.
- Both new providers registered in the conformance suite; 263 unit tests
  pass (up from 206).

### Changed
- **README rewritten** in less AI-flavored voice — leads with the demo,
  trims bullet-heavy "Crucially:" sections, replaces design-doc-style
  prose. ASCII demo block replaces the missing gif.
- **Provider env-var maps** in `server/routes/chat.py` and
  `server/routes/models.py` updated for the two new providers.
- **`knowledge/` directory replaced with `docs/`.** Personal ops notes
  (release checklist, PyPI token rotation, Daytona sandbox notes,
  smoke test runbook, full design plans) moved out of the public OSS
  repo into a private tracking repo. Public-facing subset kept under
  `docs/providers/`, `docs/agent-binders.md`, `docs/hermes.md`. All
  cross-references in source docstrings updated.

### Added (post-CF/HF)
- **Claude Code plugin + skill.** `/plugin install https://github.com/Shaivpidadi/FreeRideV3`
  loads `skills/freeride/SKILL.md` so Claude auto-detects FreeRide is
  running, wires `OPENAI_API_BASE` against it, and explains the
  `X-FreeRide-Provider` header without the user having to teach it.
  Plugin manifest at `.claude-plugin/plugin.json`.

## [0.3.0a6] — 2026-05-07

### Added
- **Groq provider** (`freeride/providers/groq.py`). OpenAI-compatible at
  `api.groq.com/openai/v1`. Free-tier detection is a hardcoded allowlist
  (Llama 3.x family, Gemma 2, Mixtral, DeepSeek-R1-distill) with a
  `GROQ_FREE_MODELS_OVERRIDE` env var for users on different plans.
  Classifier handles "model decommissioned" 400s by mapping to
  `MODEL_NOT_FOUND` so the resolver advances. Strips Groq's `x_groq`
  response extension before forwarding to clients. Auto-loaded by
  `freeride serve` when `GROQ_API_KEY` is set.
- **GitHub Actions CI** (`.github/workflows/test.yml`) — runs pytest on
  every push/PR across {ubuntu, macos} × {3.10, 3.11, 3.12, 3.13}.
- **GitHub Actions release pipeline** (`.github/workflows/release.yml`) —
  Trusted Publishing (OIDC) for tag-driven PyPI uploads. First release
  cut by this pipeline.
- **`CONTRIBUTING.md`** — Provider plugin + binder authoring guide.

## [0.3.0a5] — 2026-05-07

### Added
- **`python -m freeride` entry point** — works in any terminal regardless of
  PATH or venv activation. Drop-in fallback for the `freeride` console-script
  binary. ([install issue surfaced during external smoke test])

## [0.3.0a4] — 2026-05-07

### Fixed
- **First-run telemetry banner now prints on `freeride --help` and `--version`.**
  Argparse handles those flags before our dispatcher code ran, so the
  disclosure was being silently skipped on the most common first invocation.
  Banner check moved to before `parse_args()`.

## [0.3.0a3] — 2026-05-07

### Fixed
- **Hard-pin `httpx>=0.27,<1`.** httpx 1.0.dev3 dropped the public exception
  hierarchy (`HTTPStatusError`, `TimeoutException`, `RequestError`); our
  classifier paths reference all three. `pip install --pre` was propagating
  `--pre` to deps and pulling 1.0.dev3, breaking installs. Pin until httpx 1.0
  stabilizes and we update `classify_error`.

## [0.3.0a2] — 2026-05-07

### Added
- **Default-on telemetry with first-run disclosure banner.** Banner prints
  once before any subcommand runs, shows the exact payload that will be sent,
  and how to opt out (`freeride telemetry off`). Persists
  `telemetry_disclosure_shown` in `~/.freeride/config.json` so it never
  re-prints. Toggle: `freeride telemetry on|off`.
- **Telemetry backend** in `services/telemetry/` — Cloudflare Worker + D1.
  Routes: `POST /v1/beacon`, `GET /v1/stats`, `GET /health`, `GET /install.sh`,
  `GET /`. Public custom domain `free-ride.xyz` (apex) and
  `telemetry.free-ride.xyz` (alias).
- **Repo-root `LICENSE` file** (MIT, attribution to Shaishav Pidadi 2026).

## [0.3.0a1] — 2026-05-07

First public release. Distributable name on PyPI: `freeride-gateway`.

### Added
- **Local OpenAI-compatible gateway** that orchestrates free-tier inference
  across multiple providers, with transparent failover.
  - `freeride serve` — FastAPI + uvicorn server on `localhost:11343`
  - `POST /v1/chat/completions` — non-streaming and streaming (SSE), with
    buffer-first-chunk failover spanning providers
  - `GET /v1/models` — aggregated free-model catalog, 6h TTL cache
  - `GET /health` — `{ok, version, providers}`
- **Provider Protocol** (`freeride.core.provider.Provider`) — frozen at
  `api_version=1`. Plugins register via Python entry points.
- **Two providers** shipped:
  - OpenRouter (full surface — chat, streaming, classifier covers known
    error patterns including the typo'd-model 400 case)
  - NVIDIA NIM (curated free-model allowlist; classifier handles HTTP 403=AUTH
    and `text/plain` 404=MODEL_NOT_FOUND quirks)
- **Cross-provider failover** — provider chain walked in registration order;
  on `RATE_LIMIT`/`AUTH` advance keys, on `MODEL_NOT_FOUND` advance providers.
  Streaming uses buffer-first-chunk; once first byte ships, mid-stream errors
  surface as truncated stream (documented limitation).
- **Per-key cooldown**, persistent across CLI invocations
  (`~/.freeride/cooldown.json`, 120s TTL).
- **Atomic state writes** — every config or state file goes through
  `core/state.atomic_write` (temp + os.replace).
- **Agent binders** — `freeride bind <openclaw|aider|continue|hermes>` writes
  the agent's config to point at the gateway, preserving all unrelated keys.
  After bind, the agent works without further user steps:
  - **Aider**: writes `openai-api-base`, `openai-api-key`, and a default
    `model:` line so `aider` (no flags) just works.
  - **Hermes** (`NousResearch/hermes-agent`): writes
    `~/.hermes/config.yaml` (NOT `cli-config.yaml` — the example filename
    is misleading) plus `~/.hermes/.env` `LM_API_KEY` if no real key
    present.
  - **Continue**: appends a model entry to `~/.continue/config.yaml`
    (`provider: openai`, NOT `openai-compatible`).
  - **OpenClaw**: writes `models.providers.freeride` with the full
    `ModelDefinitionConfig` (incl. `api: "openai-completions"`) and an auth
    profile pointer; sets `agents.defaults.model.primary = "freeride/openrouter/free"`.
    Verified end-to-end with a real `openclaw agent --local` chat round-trip.
- **v2 backwards-compatibility shims** — `freeride auto/list/switch/status/
  refresh/fallbacks/rotate` preserve v2 behavior so existing v2 OpenClaw
  users get an in-place upgrade.
- **Test suite** — 175 hermetic unit tests + 6 e2e tests (real Aider, real
  Hermes, openai-python SDK, real OpenClaw chat).
- **One-line installer** at `https://api.free-ride.xyz/install.sh` — bootstraps
  `uv` if missing, runs `uv tool install --prerelease=allow freeride-gateway`,
  drops binary at `~/.local/bin/freeride`.

### Notes
- Daytona Tier 1/2 sandboxes block egress to NVIDIA endpoints AND
  free-ride.xyz at TLS — verified via direct `curl` from sandbox returns
  "Connection reset by peer" before TLS handshake completes. This is a
  Daytona network-policy restriction, not a FreeRide bug.

[Unreleased]: https://github.com/Shaivpidadi/FreeRideV3/compare/v0.4.0a2...HEAD
[0.4.0a2]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.4.0a2
[0.4.0a1]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.4.0a1
[0.3.0a8]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a8
[0.3.0a7]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a7
[0.3.0a6]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a6
[0.3.0a5]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a5
[0.3.0a4]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a4
[0.3.0a3]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a3
[0.3.0a2]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a2
[0.3.0a1]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a1
