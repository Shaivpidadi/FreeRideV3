# Changelog

All notable changes to FreeRide are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/Shaivpidadi/FreeRideV3/compare/v0.3.0a8...HEAD
[0.3.0a8]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a8
[0.3.0a7]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a7
[0.3.0a6]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a6
[0.3.0a5]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a5
[0.3.0a4]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a4
[0.3.0a3]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a3
[0.3.0a2]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a2
[0.3.0a1]: https://github.com/Shaivpidadi/FreeRideV3/releases/tag/v0.3.0a1
