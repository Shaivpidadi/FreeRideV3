# OpenAI Codex CLI

> The new agentic terminal tool from OpenAI (Rust, `npm install -g @openai/codex`). FreeRide makes it work without a paid OpenAI plan — the gateway translates Codex's Responses-API wire protocol to free upstream providers.

## TL;DR

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
npm install -g @openai/codex
freeride run codex
```

You're in a Codex session, routed through free providers. No `OPENAI_API_KEY` or `CODEX_API_KEY` needed. You don't have to sign in to OpenAI.

---

## How it works

Codex speaks **only** the OpenAI Responses API (`POST /v1/responses`) — `wire_api = "chat"` is explicitly rejected on its side. The gateway hosts a Responses-shape endpoint that:

1. Accepts the typed-item `input` array (messages, function_call, function_call_output, reasoning items)
2. Unwraps the flat `tools[]` (Responses uses `{type, name, parameters}`, Chat Completions uses `{type, function: {…}}`)
3. Translates to OpenAI Chat Completions
4. Routes through the same failover machinery `/v1/messages` and `/v1/chat/completions` use
5. Translates the response back to Responses shape (with `output[]` array of typed items, status field, no choices)
6. Re-frames streaming chunks into Responses SSE event protocol with explicit framing events (`response.output_item.added` → `response.content_part.added` → `response.output_text.delta` → … → `response.completed`)

The Codex CLI parses every event natively; from its perspective the gateway is OpenAI.

Model field on the request (`gpt-5-codex`, `gpt-5`, etc.) is rewritten to `auto` before dispatch — we don't host OpenAI's proprietary models, so the smart-router picks a free model. The originally-requested id is echoed back on the response so the CLI's UI stays coherent.

## What the wrapper does

`freeride run codex` does three things before invoking codex:

1. **Injects `-c openai_base_url=http://localhost:11343/v1` into argv** right after the `codex` binary name. Codex reads its base URL from `~/.codex/config.toml` (not an env var); the `-c` flag is the per-invocation override that doesn't mutate the user's config file.
2. **Sets `CODEX_API_KEY=sk-freeride-no-auth`** (a sentinel) if the parent env has none. Codex's auth gate refuses to make any HTTP request without this — the sentinel satisfies the local check; the gateway recognizes it via `has_inbound_auth()` and demotes it to "no auth" for routing decisions.
3. **Writes `~/.codex/auth.json`** with `{"OPENAI_API_KEY": "sk-freeride-no-auth"}` on first run if the file doesn't exist, so Codex's "configure authentication" picker doesn't fire.

Real user credentials are never touched:
- An existing `~/.codex/auth.json` is left alone.
- A real `CODEX_API_KEY` in the env passes through untouched.
- A user-supplied `-c openai_base_url=…` later in argv wins via Codex's last-write-wins config layering.

## Models

Whatever model you pick in the Codex UI gets rewritten to `auto`. The smart-router picks the healthiest free model from the catalog (currently `openrouter/owl-alpha` carries most of the traffic). To force a specific underlying provider:

```bash
freeride run codex --header X-FreeRide-Force-Provider=groq
```

(or set per-request: `curl -H 'X-FreeRide-Force-Provider: nvidia_nim' …`)

## Troubleshooting

**"unexpected status 401 Unauthorized: Incorrect API key provided"**
The CLI bypassed the gateway and hit `api.openai.com` directly. Causes:
- A user-supplied `-c openai_base_url=…` flag later in argv overrode ours. Drop the extra flag or run without our wrapper.
- The gateway isn't running on the port the wrapper expects. Check `freeride doctor` and `curl localhost:11343/health`.

**`bubblewrap: loopback: Failed RTM_NEWADDR: Operation not permitted`**
Codex uses `bubblewrap` to sandbox its shell tool. Nested-sandbox environments (some Docker images, Daytona, gVisor) block bwrap's network-namespace setup. The model still works — only the `shell` / `Bash` tool fails. Fix: install `bubblewrap` natively (`apt-get install bubblewrap` on Debian) or run codex on bare metal.

**Codex tries WebSocket first, then HTTP**
Versions 0.130.0+ attempt a WebSocket transport at `ws://<gateway>/v1/responses` before falling back to HTTP/SSE. The gateway doesn't host WebSocket yet; you'll see 5 retry lines in the log, then it falls back and works. Cosmetic.

**`request_user_input is unavailable in Default mode`**
Codex's approval system gating a tool call. Use `--full-auto` (deprecated) or `-c approval_policy="never" -c sandbox_mode="workspace-write"`.

## Telemetry signals

Each request lands as a series of events in `~/.freeride/events.jsonl`:

```json
{"type": "responses_routing_decision", "model": "gpt-5-codex", "streaming": true, "endpoint": "responses"}
{"type": "request_start", "model": "auto", "streaming": true, "endpoint": "responses"}
{"type": "auto_model_resolved", "resolved_model": "openrouter/owl-alpha", "resolved_provider": "openrouter"}
{"type": "provider_attempt", "provider": "openrouter", "key_index": 0, "model": "openrouter/owl-alpha"}
{"type": "provider_response", "status": "OK", "duration_ms": 3159}
{"type": "request_complete", "provider": "openrouter", "streaming": true}
```

`endpoint: "responses"` is the marker that this came through the Codex path (vs `messages` for Claude Code, `gemini` for the Gemini CLI, `chat_completions` for everything else).

## Caveats

* **Reasoning items** (o-series / `gpt-5-codex` thinking blocks) pass through schema-side but aren't currently translated to upstream. Free providers don't emit reasoning items either, so this is mostly a no-op.
* **Built-in tool types** (`web_search`, `custom`, `mcp`, `file_search`, `code_interpreter`) get filtered out before reaching upstream — free providers don't accept them. Function tools work.
* **`previous_response_id` stateful chaining** isn't supported (we don't persist responses). Codex falls back to including history in `input`, which works.

## Reference

* Codex CLI source: [github.com/openai/codex](https://github.com/openai/codex)
* OpenAI Responses API: [platform.openai.com/docs/api-reference/responses](https://platform.openai.com/docs/api-reference/responses)
* Our translator: [`freeride/core/codex_translate.py`](../../freeride/core/codex_translate.py)
* Our route: [`freeride/server/routes/codex.py`](../../freeride/server/routes/codex.py)
