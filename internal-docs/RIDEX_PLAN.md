# ridex: fork fx, FreeRide as the model daemon — revised plan

## Context

Rewrite of the "Stopping the fork" strategy doc with two changes folded in from review:

1. **The `freeride` provider speaks fx's existing gateway dialect, not OpenAI chat completions.** Translation moves into FreeRide (Python), where the project already maintains three wire-format translators. The Zig fork diff shrinks to enum + URL + auth + branding, which keeps rebases against upstream fx (pushed daily, 2.6k stars) cheap.
2. **Tool-call reliability is treated as the product risk, not plumbing.** Default model is the `freeride/coding` route instead of `auto`, and a second green — a full tool round-trip — becomes the go/no-go gate before daemon polish.

Everything else from the original plan stands. The revised doc:

---

**Problem.** FreeRide today is a local model pipe. `freeride serve` on `:11343`, failover across OpenRouter, Groq, NIM, HF, Cerebras, Cloudflare, Ollama. The product was binders: wrap Claude Code, Codex, Gemini, Aider, Continue, OpenClaw, Hermes. Those agents are failing. Wrapping someone else's CLI means we die when they change a flag or a protocol. Users also had to run our server in a second terminal. That is not a product.

**What we want.** Our own coding agent. You type one command. It reads, edits, and runs commands. Models come from FreeRide. No second terminal. No Vercel account required. Vendor logins stay optional.

**Why fx.** vercel-labs/fx is a small Zig agent: CLI, tools, permissions, sessions, ACP. Apache-2.0. You have no fork yet. It is model-agnostic in the README and not in the code. It talks to three providers only: Vercel AI Gateway (default), Codex OAuth, Grok OAuth. The default posts AI SDK `{prompt:[…]}` JSON to `ai-gateway.vercel.sh/v3/ai/language-model`. `FX_GATEWAY_CHAT_URL` moves the URL but not the payload shape, so an env-var bind to our `/v1/chat/completions` 400s. We fork and add a provider — but we do not teach the fork a new wire protocol (see Provider).

**Two names.** FreeRide stays the server and the model brand. The agent gets a different command. Working name `ridex` (config `~/.ridex`). We do not ship `fx` (Vercel's name; Apache-2.0 grants no trademark rights) and we do not ship `free-fx` (looks like a clone). Rename before public if you want. Repo: fork `vercel-labs/fx` to `Shaivpidadi/ridex`.

**Split.** `ridex` is the process you launch. It owns the agent loop, tools, and shell. FreeRide owns provider failover, protocol translation, and, later, stats. We do not put `run_command` behind localhost HTTP. The daemon is a model shim, not an agent.

**Provider.** Stock CLI is `fx provider gateway|codex|grok`, a closed enum. We add `freeride` as a fourth provider and make it the default: `ridex provider freeride|gateway|codex|grok`. We do not overwrite `gateway` and pretend it is us. `gateway` stays Vercel, optional. Codex and Grok stay optional.

The `freeride` provider **reuses fx's existing gateway transport and wire code** (`src/gateway/vercel_protocol.zig`, `src/gateway/client.zig`) pointed at `http://localhost:11343`, dummy auth, no Vercel login, no credits check. We write **zero new wire-format code in Zig**. The Zig diff is: provider enum entry, URL/auth resolution for the freeride path, branding strings. Small patch = cheap upstream rebases; upstream moves daily and carrying an OpenAI SSE parser + tool-call delta accumulator in a fork is the binder-fragility problem reborn as merge conflicts.

The translation burden moves to FreeRide, which is already a translator shop (Anthropic, Codex Responses, Gemini). FreeRide grows a fourth dialect: the fx gateway protocol.

- New route `freeride/server/routes/fx.py` (mounted in `freeride/server/app.py` beside the other five routers): chat endpoint accepting fx's `{prompt:[…], tools, toolChoice, maxOutputTokens}` request body, plus a models-catalog endpoint in the shape fx's catalog fetch expects.
- New translator `freeride/core/fx_translate.py`: fx dialect ⇄ internal chat shape, both directions, streaming included. Same pattern as `codex_translate.py` / `gemini_translate.py`.
- Failover, cooldowns, health ordering, structured 503s all come free via `freeride/core/failover.py` — nothing new there.
- The executable spec for the dialect: fx's `vercel_protocol.zig` (build + parse code and its inline tests) and fx's own e2e mock servers under `tests/e2e/`, which their CI drives via `FX_GATEWAY_CHAT_URL`. We build FreeRide's translator against those fixtures, and we can run fx's e2e suite pointed at a live FreeRide as an integration check.

**Model default.** The freeride provider defaults to the **`freeride/coding`** route (`freeride/core/model_router.py` presets — pinned to models that reliably emit tool calls), not `auto`. `auto` optimizes availability; an agent loop lives or dies on tool-call reliability, and free-tier catalogs are exactly where that is weakest. Stock fx defaults to `moonshotai/kimi-k3` for the same reason. `ridex models` lists the full catalog; users can switch per session.

**Daemon.** Hard requirement for this cut. Users never open a second terminal and never type `freeride serve`. Install starts FreeRide in the background. It stays up. Crash restarts it. `ridex start|stop|restart` and `ridex doctor` exist. Stop sticks. The next `ridex` ask does not sneak it back on. `ridex start` or `ridex restart` brings it back. If `:11343` is dead because they never installed or they stopped it, `ridex` says so. It does not hang on a cold Python boot every prompt.

**Keys.** If provider keys are missing, we prompt on their machine. `ridex` detects the condition (empty catalog or the structured 503 tried-list from the daemon) and shells out to the existing `freeride init`, then triggers a reload — both already exist in the Python codebase; we do not reimplement key prompting in Zig. OpenRouter, Groq, and the rest stay local. Never paste keys in this chat. Never send keys to the DB.

**Stats.** Later. Daemon flushes models, provider, tokens, success/fail. No prompts, no repo paths, no file contents.

**Not this cut.** Binders as the product. Zig rewrite of FreeRideV3. Using FreeRide `/v1/responses` (that is the Codex wrap). Interactive TUI polish, ACP, Vercel web-search tools. Trademark-safe public rename if `ridex` is only a working name.

**First green (plumbing).** `ridex ask "reply with the single word pong"` with no Vercel login and no second terminal. FreeRide is already a daemon. Request hits `:11343` in the fx gateway dialect. `ridex models` lists the catalog. Missing keys: a local prompt, then it runs.

**Second green (product — go/no-go gate).** `ridex ask "create hello.txt containing hi"`. A full tool round-trip: fx sends its tool schemas, FreeRide translates, a free-tier model emits a correct tool call, fx executes it, the result goes back, the model finishes. Run it repeatedly across the coding route's model set and measure the pass rate. Pong proves the pipe; this proves the product. If no free model passes reliably, we stop and rethink before spending on daemon polish — that failure would invalidate the pivot, and we want to know in week one, not after the supervisor work.

**Why this order.** Add the provider or the agent cannot talk to us. Prove tool calls next because that is the only step that can kill the product; everything after it is plumbing. Background the server or normal users will not run it. Ask for keys locally or the daemon is a brick. Daemonize-and-prompt is in the first cut because you said it has to be. Stats and binders wait.

**Verdict.** Fork fx, call the agent `ridex`, default provider `freeride` speaking fx's own gateway dialect to localhost, translation in FreeRide where the translators already live, default model the coding route, hide FreeRide as a daemon, prompt for keys on the machine, and gate the whole bet on one tool round-trip passing on free models. That is the product. Wrapping Claude is not.

---

## Implementation notes (for when this plan executes)

Key files:

- **fx fork (Zig, minimal diff):** provider enum/catalog (`src/core/auth/provider_catalog.zig`, `src/core/gateway/provider_set.zig`), URL/auth resolution (`src/builtins/gateway.zig` — `resolveChatUrlForProvider`, `validateApiKey`), branding. Reuse `src/gateway/vercel_protocol.zig` and `src/gateway/client.zig` untouched.
- **FreeRide (Python, where the real work lands):** new `freeride/core/fx_translate.py` + `freeride/core/fx_schema.py`, new `freeride/server/routes/fx.py`, router wired in `freeride/server/app.py:225-230`. Reuse `freeride/core/failover.py` (`try_call_with_failover`, `try_stream_with_failover`), `freeride/core/model_router.py` presets, `freeride/core/auto_model.py`.
- **Daemon + keys:** supervisor per OS (launchd / systemd user unit; decide Windows scope explicitly), `ridex start|stop|restart|doctor`; keys flow shells out to `freeride init` + reload (`freeride/cli/cmd_init.py`, `cmd_reload.py`).

## Verification log

**2026-09-01 — FreeRide side landed and live-verified** (fx dialect
routes: `POST /v3/ai/language-model`, `GET /coding-agent/v1/models`):

- `GET /coding-agent/v1/models` with a dummy Bearer → 200, presets
  first + 145 live catalog models. This is fx's key-validation probe.
- Streaming pong through `freeride/coding` (pinned to openrouter/free)
  → correct fx event framing (`response-metadata` → `text-delta` →
  `finish{unified:stop}` → `[DONE]`).
- Streaming tool call → full `tool-input-start/delta/end` →
  `tool-call` with parsed input object → `finish{unified:tool-calls}`.
- Multi-turn history with an fx `tool-result` part → model reads the
  result and answers in text.
- Non-streaming (`ai-language-model-streaming: false`) → plain OpenAI
  `choices[0].message` JSON, which is what fx's
  `parseGatewayCompletion` reads.
- **Go/no-go gate: 10/10 tool-call pass rate** on the
  "create hello.txt containing hi" benchmark via the coding pin.
  Free-tier models can drive the loop; the pivot stands.
- Note from setup: the OpenRouter key in `~/.freeride/.env` was stale
  ("User not found") while the working key sat in the repo `.env` the
  gateway never reads — exactly the keys-UX failure the ridex
  `doctor` + `freeride init` flow must catch.

**2026-09-01 — ridex fork landed, both greens pass** (local repo
`~/Desktop/oss/ridex`, branch `feat/freeride-provider`, not pushed):

- `.freeride` is a real fourth `ProviderId` and the default. Diff:
  +133/−10 over 18 files (plus launcher/tests) — no new wire code; the
  stock gateway transport is reused with URL resolution switched on a
  core-level `active_transport_provider` published from the provider
  runtime's `adoptOwned` boundary. Credential is synthetic
  (`freeride-local`), so no login gates the agent. Vercel
  gateway/Codex/Grok stay selectable and untouched.
- **Green 1**: `fx ask "reply with the single word pong"` → `pong`
  (~2s, no Vercel login, no env vars, no second terminal).
- **Green 2**: `fx ask "create hello.txt containing hi"` → agent
  emitted the write_file tool call, executed it, read the file back to
  verify, and reported completion (~15s end to end).
- Fork test suite matches the stock-fx baseline exactly (8661/8668;
  the same 5 environment-dependent `run_command` failures fail on
  unmodified upstream on this machine).
- `scripts/ridex` launcher: auto-starts the FreeRide daemon, `ridex
  start|stop|restart|doctor`, stop-sticks marker, socket-probe drains,
  local `freeride init` prompt when `/health.keyed_providers` is empty.
- FreeRide fixes found by the integration: `/health` gained
  `keyed_providers`; serve's port probe now binds with `SO_REUSEADDR`
  (stop-then-start raced TIME_WAIT); the fx stream route ships headers
  immediately with `: preflight` keepalive comments so free-tier TTFB
  no longer trips fx's first-byte timeout (previously every turn ate
  1–2 visible retries).
- Still open for later cuts: real crash-restart supervision
  (launchd/systemd units — today a crashed daemon restarts on the next
  ridex command), the public `ridex` rename (binary still builds as
  `fx`; internal `FX_*` env names kept for cheap upstream rebases),
  stats flush, Windows.

## Verification

1. **Translator unit tests** (`tests/test_fx_translate.py`): request/response/stream fixtures captured from `vercel_protocol.zig` inline tests and fx's e2e mocks; hermetic, same style as `test_codex_translate.py`.
2. **Conformance:** run fx's own e2e suite with `FX_GATEWAY_CHAT_URL` / `FX_GATEWAY_BASE_URL` pointed at a live local FreeRide.
3. **Green 1:** `ridex ask "reply with the single word pong"` — no Vercel login, no second terminal.
4. **Green 2 (gate):** `ridex ask "create hello.txt containing hi"` — repeated runs across the coding-route models; record pass rate; go/no-go before daemon investment.
5. **Daemon:** kill the process → auto-restart; `ridex stop` → next ask reports dead daemon and does not restart it; `ridex doctor` reports port, version, key status.
