# Google Gemini CLI

> The official terminal agent from Google (`npm install -g @google/gemini-cli`). FreeRide makes it work without a Google Cloud account or `GEMINI_API_KEY` — the gateway translates Google's Generative Language API protocol to free upstream providers.

## TL;DR

```bash
curl -sSL https://api.free-ride.xyz/install.sh | sh
npm install -g @google/gemini-cli
freeride run gemini
```

Or for one-shot non-interactive:

```bash
freeride run gemini --skip-trust --prompt 'explain this repo'
```

You're in a Gemini session, routed through free providers. No `GEMINI_API_KEY` or Google sign-in needed.

---

## How it works

Gemini CLI speaks Google's native Generative Language API — **not** OpenAI-compatible. Requests go to URLs like:

```
POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent
POST https://generativelanguage.googleapis.com/v1beta/models/<model>:streamGenerateContent?alt=sse
```

Body shape is `{contents: [...], tools: [{functionDeclarations: [...]}], generationConfig, systemInstruction}` — different keys, different nesting from OpenAI.

The gateway hosts the same path shapes locally and:

1. Parses Google's camelCase JSON (via Pydantic's `to_camel` alias generator)
2. Flattens `Tool.functionDeclarations[]` into OpenAI's `tools[]`
3. Maps `tool_config.function_calling_config.mode` (`AUTO`/`NONE`/`ANY`) → OpenAI `tool_choice` (`auto`/`none`/`required`)
4. Hoists `systemInstruction` into a leading `role=system` message
5. Handles both newer (`functionResponse` under `role=user`) and legacy (`role=function`) tool-result conventions
6. Routes through the same failover machinery as everything else
7. Translates the response back to `{candidates, usageMetadata, modelVersion}` shape
8. Streams in Google's "incremental complete responses" SSE format (each chunk a full GeminiGenerateResponse with partial content)

Model field in the URL (`gemini-2.0-flash`, `gemini-2.5-pro`, etc.) is rewritten to `auto` before dispatch — we don't host Google's proprietary models. The originally-requested model id is echoed back as `modelVersion` on the response so the CLI's UI stays coherent.

## What the wrapper does

`freeride run gemini` does two things before invoking gemini-cli:

1. **Sets `GOOGLE_GEMINI_BASE_URL=http://localhost:11343`** so the CLI's `@google/genai` SDK routes all calls to the gateway. (No `/v1` suffix — the SDK appends path segments itself.)
2. **Sets `GEMINI_API_KEY=sk-freeride-no-auth`** (a sentinel) if the parent env has neither `GEMINI_API_KEY` nor `GOOGLE_API_KEY`. Newer gemini-cli versions ship an `AuthType.GATEWAY` path that allows empty keys when the base URL is set, but 0.42.0 and earlier still require an env-var to be present — otherwise the CLI short-circuits with "Please set an Auth method" before making any HTTP request. The sentinel satisfies this gate; the gateway treats it as no-auth via `has_inbound_auth()`.

On first run, the wrapper also writes `~/.gemini/settings.json` with `{"selectedAuthType": "gemini-api-key"}` so the interactive auth-method picker doesn't fire. An existing settings.json is left alone — a user who ran `gemini login` previously keeps their state.

## Models

Whatever `gemini-*` model the CLI sends gets rewritten to `auto`. The smart-router picks the healthiest free model from the catalog. Currently most traffic resolves to `openrouter/owl-alpha`. To force a specific provider:

```bash
# header on the next request
curl -H 'X-FreeRide-Force-Provider: groq' …
```

## Non-interactive modes

The CLI's first-run setup expects an interactive TTY for the workspace-trust prompt. In headless / CI contexts, pass `--skip-trust` (or set `GEMINI_CLI_TRUST_WORKSPACE=true`):

```bash
freeride run gemini --skip-trust --prompt 'list files'
```

The wrapper's banner suppresses automatically when stdin isn't a TTY.

## Troubleshooting

**"Please set an Auth method"**
The wrapper's sentinel didn't get injected. Causes:
- A previous run wrote `~/.gemini/settings.json` with `"selectedAuthType": "oauth-personal"` or similar. Either rerun the wrapper after `rm ~/.gemini/settings.json`, or set `GEMINI_API_KEY` explicitly.
- Wrapper invoked without `freeride run` and only `GOOGLE_GEMINI_BASE_URL` was set manually. Add `GEMINI_API_KEY=any-string` to fix.

**"Gemini CLI is not running in a trusted directory"**
Workspace-trust gate. Use `--skip-trust` flag or set `GEMINI_CLI_TRUST_WORKSPACE=true`.

**`Error executing tool list_directory: params must have required property 'dir_path'`**
The model emitted a tool call with the wrong argument schema. Common with free models that have weaker tool-call fidelity than Gemini's own. Try `freeride run gemini -m gemini-2.5-pro` (the CLI passes that as a hint; under the hood we still pick a free model, but some are more reliable for tool-call shapes).

**`Error: Cannot read property '...' of undefined` on startup**
Indicates the SDK couldn't parse our response. Usually means a free model emitted a non-JSON tool call. Check `~/.freeride/events.jsonl` for the request that failed; the issue is upstream of the gateway.

## Telemetry signals

Each request lands as events in `~/.freeride/events.jsonl`:

```json
{"type": "gemini_routing_decision", "model": "gemini-2.5-pro", "streaming": true, "endpoint": "gemini"}
{"type": "request_start", "model": "auto", "streaming": true, "endpoint": "gemini"}
{"type": "auto_model_resolved", "resolved_model": "openrouter/owl-alpha", "resolved_provider": "openrouter"}
{"type": "provider_attempt", "provider": "openrouter", "key_index": 0}
{"type": "provider_response", "status": "OK", "duration_ms": 4803}
{"type": "request_complete", "provider": "openrouter", "streaming": true}
```

`endpoint: "gemini"` is the marker for the Gemini path (vs `messages` for Claude Code, `responses` for Codex).

## Caveats

* **Multimodal parts** (`inlineData`, `fileData` for images / audio) pass through the schema but aren't currently surfaced upstream — most free providers are text-only. Picking a multimodal model id may surface partial support, but the safer assumption is text-only.
* **`top_k`** has no OpenAI equivalent. Translator drops it; if you depend on top-k sampling, use a `top_p` value that approximates it.
* **Vertex AI mode** (`GOOGLE_GENAI_USE_VERTEXAI=true`) isn't routed through us — the CLI hits Vertex AI directly. Unset the env var to route through FreeRide.

## Reference

* Gemini CLI source: [github.com/google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli)
* Google Generative Language API: [ai.google.dev/api/generate-content](https://ai.google.dev/api/generate-content)
* Our translator: [`freeride/core/gemini_translate.py`](../../freeride/core/gemini_translate.py)
* Our route: [`freeride/server/routes/gemini.py`](../../freeride/server/routes/gemini.py)
