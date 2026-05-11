# Claude Code compatibility — feasibility

Last updated: 2026-05-09
Status: DRAFT for sign-off — no code yet
Owner: Shaiv
Sister docs: `MULTI_PROVIDER_SIGNUP_FEASIBILITY.md`, `AGENT_DISTRIBUTION_FEASIBILITY.md` (both in this folder; not yet committed at the time of writing — referenced where applicable)

---

## Path note

The task brief specified writing this doc to
`FreeRideV3/internal-docs/CLAUDE_CODE_COMPATIBILITY_FEASIBILITY.md`. That
folder does not exist. All FreeRide planning docs live in
`freeride-web/internal-docs/` (the private repo). This file is placed
there to match the existing convention. Move/rename if the canonical
home changes.

---

## At-a-glance summary

| Question | Answer |
|---|---|
| Is this technically feasible? | **Yes** — Claude Code's `ANTHROPIC_BASE_URL` is documented and stable; the Messages API is a published spec; multiple OSS shims (claude-code-router 33.7k★, claude-code-proxy, LiteLLM) prove the translation. |
| Is the translation hard? | **Medium-hard.** Non-streaming chat is a one-day affair. Streaming SSE is the gnarly part — Anthropic has ~7 event types, OpenAI has flat chunks. Tool use (esp. streaming `input_json_delta`) is the 80% of the bug surface. |
| Will free-tier model **quality** be good enough? | **No, not for default Claude Code workloads.** The polyglot benchmark gap is brutal: Claude Opus is in the 70s-80s%, GPT-5-high is 88.0%, but Llama-4-Maverick is **15.6%** and Gemma-3-27B is **4.9%**. Free models will produce broken edits Claude Code can't apply. We need a curated "known-good" preset that excludes the cheap garbage. |
| Is it allowed by Anthropic ToS? | **Yes.** Anthropic's own docs document `ANTHROPIC_BASE_URL` and explicitly support LLM gateways (LiteLLM, Kong, internal proxies). We don't redistribute Claude Code; the user installs it from npm and we just configure env vars. |
| Verdict | **Yes, with caveats** — see §10. Build it, but ship Phase 1+2 as a *technical preview* and gate Phase 4 (`freeride bind claude-code`) behind a curated model preset. |
| Total effort | **~10–14 engineer-days** across 4 phases, plus ~2 days of polyglot/Claude-Code-specific QA. |
| Biggest risk | Streaming tool-use translation — `input_json_delta` partial-JSON framing. If we get it wrong, Claude Code freezes mid-edit and the user blames FreeRide. |
| Headline files | `freeride/server/routes/messages.py` (new), `freeride/core/anthropic_translate.py` (new), `freeride/binders/claude_code.py` (new). |
| Next step | Spec out Phase 1 in a 1-pager (request/response field map only), validate against `claude --print` against a stub server, then start coding. |

---

## 1. How Claude Code's backend is configured

Source of truth: `https://code.claude.com/docs/en/llm-gateway` and `https://code.claude.com/docs/en/env-vars` (the `docs.anthropic.com/en/docs/claude-code/...` URLs 301-redirect to `code.claude.com/docs/en/...` as of 2026).

### Verified env vars

| Env var | Behavior |
|---|---|
| `ANTHROPIC_BASE_URL` | "Override the API endpoint to route requests through a proxy or gateway." When set to a non-first-party host, MCP tool search is disabled by default; set `ENABLE_TOOL_SEARCH=true` if your proxy forwards `tool_reference` blocks. |
| `ANTHROPIC_AUTH_TOKEN` | "Custom value for the `Authorization` header (the value you set here will be prefixed with `Bearer `)." |
| `ANTHROPIC_API_KEY` | "API key sent as `X-Api-Key` header." When set, this overrides the user's Pro/Max subscription. |
| `ANTHROPIC_CUSTOM_HEADERS` | `Name: Value` newline-separated extras. |
| `ANTHROPIC_MODEL` | Active model name/alias. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` / `_SONNET_MODEL` / `_HAIKU_MODEL` | What `opus`/`sonnet`/`haiku` aliases resolve to. |
| `ANTHROPIC_SMALL_FAST_MODEL` | **Deprecated** as of recent Claude Code; replaced by `ANTHROPIC_DEFAULT_HAIKU_MODEL`. |
| `ANTHROPIC_CUSTOM_MODEL_OPTION` | Adds a custom model entry to the `/model` picker. Validation skipped — accepts any string. |
| `CLAUDE_CODE_ATTRIBUTION_HEADER=0` | Strips Claude Code's prepended attribution block from the system prompt → improves prompt-cache hit rates on a third-party gateway. |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` | Strips `anthropic-beta` headers and beta tool-schema fields like `defer_loading`, `eager_input_streaming`. **Use this for FreeRide** so we don't have to implement betas. |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` | If we expose a gateway-format `/v1/models`, Claude Code will populate its `/model` picker from it. **Big deal for UX** — users can `/model auto`. Requires Claude Code v2.1.129+. |

### Real key required?

**No.** Static dummy keys work. The doc shows: `ANTHROPIC_AUTH_TOKEN=sk-litellm-static-key` for LiteLLM. We can use literally any string — `ANTHROPIC_AUTH_TOKEN=freeride-local` is fine. The token is forwarded to FreeRide as the `Authorization: Bearer ...` header; we ignore it (since FreeRide is local-only).

### Claude Code's gateway requirements (binding constraints on us)

From `code.claude.com/docs/en/llm-gateway`, three gateway formats are accepted. The relevant one for us:

> 1. **Anthropic Messages**: `/v1/messages`, `/v1/messages/count_tokens`
>    Must forward request headers: `anthropic-beta`, `anthropic-version`

- We must implement `POST /v1/messages` (streaming + non-streaming).
- We must implement `POST /v1/messages/count_tokens` (best-effort — we can return a heuristic estimate).
- We must forward the `anthropic-beta` and `anthropic-version` headers (or strip them if we set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in our binder).
- Claude Code adds an `X-Claude-Code-Session-Id` header — we should propagate it to our event log for debugging.

### Version specifics

- Opus 4.7 requires Claude Code v2.1.111+. (Doesn't affect us — we map down to free models.)
- Gateway model discovery requires v2.1.129+.
- The migration of `ANTHROPIC_SMALL_FAST_MODEL` → `ANTHROPIC_DEFAULT_HAIKU_MODEL` happened recently; our binder should set the new var.

**Doc URLs:**
- LLM gateway: `https://code.claude.com/docs/en/llm-gateway`
- Env vars: `https://code.claude.com/docs/en/env-vars`
- Model config: `https://code.claude.com/docs/en/model-config`

---

## 2. Anthropic Messages API — what we need to implement

Sources:
- `https://platform.claude.com/docs/en/api/messages` (top-level schema)
- `https://platform.claude.com/docs/en/api/messages-streaming` (SSE event flow)

### Required top-level request fields

| Field | Required | Purpose |
|---|---|---|
| `model` | yes | We translate this to a free-tier model id. |
| `messages` | yes | Array of `{role, content}`. `content` is either a string or an array of content blocks. |
| `max_tokens` | yes | OpenAI's `max_tokens` (or `max_completion_tokens`). |
| `system` | optional | Top-level string OR array of `TextBlockParam`. **Translates to a leading OpenAI `role: system` message.** |
| `temperature` | optional | Pass-through. |
| `top_p` / `top_k` | optional | `top_p` pass-through; `top_k` not in OpenAI Chat Completions — drop. |
| `stop_sequences` | optional | OpenAI `stop` array. |
| `stream` | optional | Streaming on/off. |
| `tools` | optional | Anthropic tool defs (see §3). |
| `tool_choice` | optional | Anthropic shape: `{type: "auto"|"any"|"tool"|"none", name?}`. |
| `metadata` | optional | `{user_id?}`. We can drop or pass to provider. |
| `thinking` | optional | `{type: "enabled"|"disabled"|"adaptive", budget_tokens, display}`. **Drop for free-tier; doesn't translate.** |
| `cache_control` | optional | Anthropic prompt caching. **No equivalent on OpenAI; drop.** |
| `service_tier`, `inference_geo`, `container`, `output_config` | optional | Drop — Anthropic-specific. |

### Required response fields

```
{
  "id": "msg_...",          // we generate
  "type": "message",
  "role": "assistant",
  "model": "<echoed>",
  "content": [<block>, ...], // array of blocks
  "stop_reason": "end_turn" | "max_tokens" | "stop_sequence" | "tool_use" | "pause_turn" | "refusal",
  "stop_sequence": null | "<which>",
  "usage": {
    "input_tokens": int,
    "output_tokens": int,
    "cache_creation_input_tokens": int (optional),
    "cache_read_input_tokens": int (optional)
  }
}
```

### Content block types we must support (input)

(`MessageParam.content` array entries)

| Block type | Notes |
|---|---|
| `text` | `{type: "text", text}` — trivial, maps to OpenAI string content. |
| `image` | `{type: "image", source: {type: "base64"|"url", media_type, data}}` — translates to OpenAI's `{type: "image_url", image_url: {url: "data:..."}}`. Only works if the underlying provider supports vision (most free models don't). |
| `tool_use` | (assistant role) `{type: "tool_use", id, name, input}` — translates to OpenAI `tool_calls` array. |
| `tool_result` | (user role) `{type: "tool_result", tool_use_id, content, is_error}` — translates to OpenAI `{role: "tool", tool_call_id, content}`. |
| `document` | PDF/text doc input. **Skip in v1** — free models don't support; raise a clean 400 if seen. |
| `thinking` / `redacted_thinking` | Extended thinking blocks. **Drop on input** (free models can't reason about them); **never emit on output**. |
| `search_result`, `*_tool_result`, `container_upload` | Anthropic server-side tool results (web_search_20250305, code_execution, etc). **Reject in v1** with a clear 400 — these require Anthropic-side infra we can't replicate. |

### SSE event flow (streaming)

Confirmed from `messages-streaming` docs:

```
1. message_start         — Message shell with empty content[], usage seeded
2. (per content block)
   content_block_start   — block index + initial shape (e.g. {type: "text", text: ""})
   content_block_delta   — one or more, delta type varies by block type
   content_block_stop    — index
3. message_delta         — top-level updates (stop_reason, stop_sequence, usage cumulative)
4. message_stop
```

**Delta sub-types:**

| Delta type | When | Shape |
|---|---|---|
| `text_delta` | text block | `{type: "text_delta", text}` |
| `input_json_delta` | tool_use block | `{type: "input_json_delta", partial_json}` — partial JSON string fragments. **Accumulate then parse on `content_block_stop`.** |
| `thinking_delta` | thinking block | `{type: "thinking_delta", thinking}` — drop. |
| `signature_delta` | thinking block trailer | `{type: "signature_delta", signature}` — drop. |
| `citations_delta` | text block w/ citations | drop in v1. |

`ping` events can appear anywhere — we should emit them periodically (~10s) to keep the connection alive through proxies.

`error` events: `event: error\ndata: {"type":"error","error":{"type":"...","message":"..."}}\n\n` — we use this for upstream failures after the first chunk has shipped.

### Stop reasons → OpenAI mapping

| Anthropic | OpenAI `finish_reason` |
|---|---|
| `end_turn` | `stop` |
| `max_tokens` | `length` |
| `stop_sequence` | `stop` (with `stop_sequence` populated) |
| `tool_use` | `tool_calls` |
| `pause_turn` | (translate from a hypothetical OpenAI streaming pause — rare on free providers; map to `stop` for safety) |
| `refusal` | `content_filter` |

### Usage mapping

| Anthropic | OpenAI |
|---|---|
| `usage.input_tokens` | `usage.prompt_tokens` |
| `usage.output_tokens` | `usage.completion_tokens` |
| `usage.cache_creation_input_tokens` | (no equivalent — drop) |
| `usage.cache_read_input_tokens` | (no equivalent — drop) |

In streaming, `message_delta.usage` is **cumulative** per Anthropic docs ("The token counts shown in the `usage` field of the `message_delta` event are cumulative"). OpenAI streaming chunks don't carry usage by default — only the final chunk if `stream_options: {include_usage: true}` is set. Our shim must request `include_usage` upstream and accumulate.

---

## 3. Translation layer — Anthropic Messages ↔ OpenAI Chat Completions

This is the heart of the feature. The translation has four sub-problems:

### 3.1 Non-streaming request

Anthropic `POST /v1/messages` body → OpenAI `POST /v1/chat/completions` body:

```
{                                          {
  "model": "claude-sonnet-4-6",   ──────►    "model": "<resolved-free-model>",
  "system": "You are...",         ──┐
  "messages": [                       │      "messages": [
    {"role": "user",                  └──►     {"role": "system", "content": "You are..."},
     "content": "..."},               ──►      {"role": "user", "content": "..."},
    ...                                        ...
  ],                                          ],
  "max_tokens": 1024,             ──────►    "max_tokens": 1024,
  "temperature": 0.7,             ──────►    "temperature": 0.7,
  "top_p": 0.9,                   ──────►    "top_p": 0.9,
  "stop_sequences": ["\n\n"],     ──────►    "stop": ["\n\n"],
  "tools": [...],                 ──────►    "tools": [...],   (see §3.3)
  "tool_choice": {"type":"auto"}, ──────►    "tool_choice": "auto",  (see §3.3)
  "stream": false                 ──────►    "stream": false
}                                          }
```

Drop: `metadata`, `thinking`, `cache_control`, `service_tier`, `inference_geo`, `container`, `output_config`.

### 3.2 Non-streaming response

OpenAI:

```json
{
  "id": "chatcmpl-...",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello!" | null,
      "tool_calls": [...] | null
    },
    "finish_reason": "stop" | "length" | "tool_calls" | "content_filter"
  }],
  "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
}
```

→ Anthropic:

```json
{
  "id": "msg_<gen>",
  "type": "message",
  "role": "assistant",
  "model": "<echo Anthropic-style id from the request>",
  "content": [
    {"type": "text", "text": "Hello!"},                  // if content
    {"type": "tool_use", "id": "...", "name": "...",     // for each tool_call
     "input": <parsed JSON>}
  ],
  "stop_reason": "end_turn" | "max_tokens" | "tool_use" | "stop_sequence" | "refusal",
  "stop_sequence": null,
  "usage": {"input_tokens": 10, "output_tokens": 5}
}
```

Edge cases:
- If `content` is empty AND `tool_calls` is non-empty → `stop_reason: "tool_use"`, content array contains only tool_use blocks. (No empty text block.)
- If `content` is non-empty AND `tool_calls` is non-empty (legitimate Anthropic shape) → emit text block FIRST, then tool_use blocks.
- If `tool_calls[].function.arguments` is malformed JSON (Llama edit-format problem — see §6) → emit a tool_use with `input: {}` and log a warning. Don't 500.

### 3.3 Tool definitions and tool calls

**Tool definitions** (request side):

Anthropic:
```json
{"name": "get_weather", "description": "...",
 "input_schema": {"type": "object", "properties": {...}, "required": [...]}}
```

OpenAI:
```json
{"type": "function",
 "function": {"name": "get_weather", "description": "...",
              "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
```

Translation: rename `input_schema` → `function.parameters`, wrap in `{type: "function", function: ...}`.

**Tool choice:**

| Anthropic | OpenAI |
|---|---|
| `{"type": "auto"}` | `"auto"` |
| `{"type": "any"}` | `"required"` |
| `{"type": "tool", "name": "X"}` | `{"type": "function", "function": {"name": "X"}}` |
| `{"type": "none"}` | `"none"` |

**Tool result blocks → tool-role messages:**

A user message with `content: [{"type": "tool_result", "tool_use_id": "toolu_xxx", "content": "result", "is_error": false}]`
becomes an OpenAI message `{"role": "tool", "tool_call_id": "toolu_xxx", "content": "result"}`.

If `is_error: true` we prefix the content with `Error: ` (no first-class error flag on OpenAI).

If `tool_result.content` is itself an array of blocks (e.g. `[{type:"text",text:"..."}]`), we flatten to a string. If it contains `image` blocks (some agents return screenshots), we either drop them with a warning (most providers) or pass through as multimodal user content (rare — needs vision-capable backend; reject in v1).

### 3.4 Streaming — the gnarly part

This is the key engineering risk. The mapping is **stateful**: we have a flat OpenAI chunk stream and must reconstruct Anthropic's structured event tree.

**State machine** the shim must maintain per request:
- Per content-block index, current state: `not_started | open_text | open_tool_use | closed`.
- For tool_use blocks: a **partial JSON accumulator** per tool_call index (OpenAI streams `tool_calls[i].function.arguments` as raw fragments; we re-emit them as `input_json_delta`).
- A `usage` accumulator (OpenAI sends usage only in the final chunk if `stream_options.include_usage=true`).

**Event timeline (non-tool case):**

OpenAI stream:
```
chunk: {choices:[{delta:{role:"assistant"}}]}            // role-only first chunk
chunk: {choices:[{delta:{content:"Hel"}}]}
chunk: {choices:[{delta:{content:"lo!"}}]}
chunk: {choices:[{finish_reason:"stop"}]}
chunk: {usage:{prompt_tokens:10,completion_tokens:5}}    // include_usage
[DONE]
```

Translates to:
```
event: message_start  data: {"type":"message_start","message":{"id":"msg_...","role":"assistant","content":[],"model":"...","usage":{"input_tokens":0,"output_tokens":0}}}
event: content_block_start  data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
event: content_block_delta  data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}
event: content_block_delta  data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo!"}}
event: content_block_stop   data: {"type":"content_block_stop","index":0}
event: message_delta        data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":5}}
event: message_stop         data: {"type":"message_stop"}
```

Note: `message_start` must ship BEFORE the first text chunk arrives, with empty content. `input_tokens` is unknown until the final chunk arrives — we either send it as 0 in `message_start` and update via `message_delta` (allowed: `usage` in `message_delta` is "cumulative"), or buffer the first chunk until we have a token count.

**Event timeline (tool-use case):**

OpenAI stream:
```
chunk: {choices:[{delta:{tool_calls:[{index:0,id:"call_abc",type:"function",function:{name:"get_weather",arguments:""}}]}}]}
chunk: {choices:[{delta:{tool_calls:[{index:0,function:{arguments:"{\"loc"}}]}}]}
chunk: {choices:[{delta:{tool_calls:[{index:0,function:{arguments:"ation\":\"SF\"}"}}]}}]}
chunk: {choices:[{finish_reason:"tool_calls"}]}
[DONE]
```

→ Anthropic:
```
message_start
content_block_start  index=0  content_block={type:"tool_use",id:"call_abc",name:"get_weather",input:{}}
content_block_delta  index=0  delta={type:"input_json_delta",partial_json:"{\"loc"}
content_block_delta  index=0  delta={type:"input_json_delta",partial_json:"ation\":\"SF\"}"}
content_block_stop   index=0
message_delta        delta={stop_reason:"tool_use",stop_sequence:null}
message_stop
```

Tricky bits:
- The **first chunk** with the tool_call id/name becomes `content_block_start`. Subsequent argument chunks become `input_json_delta`.
- If the model interleaves text + tool_calls in the same stream (rare on free-tier — most just emit one tool_call), we need to manage two indices: text at index 0, tool_use at index 1. **Watch out** for OpenAI's `tool_calls[].index` field — that's the tool call's index, separate from our content block index.
- If the upstream stream dies after the first chunk — we must emit `content_block_stop`, `message_delta` with `stop_reason: "end_turn"` (best guess), and `message_stop` to keep Claude Code from hanging. (FreeRide already has buffer-first-chunk failover, so pre-first-chunk failures retry transparently. Mid-stream failures are rare but real.)

### 3.5 Vision

Anthropic `image` block:
```json
{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR..."}}
```

OpenAI:
```json
{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}
```

Or `{"type": "image", "source": {"type": "url", "url": "https://..."}}` →
`{"type": "image_url", "image_url": {"url": "https://..."}}`.

**Provider support:** Most free-tier models don't support vision. If the request contains an image and the resolved model doesn't, we have two options:
1. Drop the image with a warning text block injected ("[image dropped: backend doesn't support vision]"). Lossy but doesn't break.
2. 400 with a clear error message.

Recommend (2) for v1 — silent dropping causes worse user experiences than a clear failure.

### 3.6 Thinking / reasoning blocks

Free-tier providers mostly don't expose reasoning blocks. DeepSeek-R1 does (via `<think>...</think>` tags in content), some OpenRouter models surface a `reasoning` field.

**Recommendation v1:** drop entirely. Don't emit thinking blocks on output, don't accept them on input. Document this. If the user *really* wants reasoning, they're not the target user.

**Recommendation v2 (later):** if the resolved model is DeepSeek-R1 family, re-pack `<think>...</think>` content into a `thinking` block. Need to mint a fake `signature` field (Anthropic uses it for integrity verification of caller-provided thinking blocks; we don't have the original signature, so this is best-effort).

### 3.7 Cache control

Anthropic `cache_control: {"type": "ephemeral"}` markers on system / user / tool blocks → no equivalent on OpenAI.

**Drop silently.** Note in docs that prompt caching is lost. The perf hit is real (Anthropic's prompt cache is a 90% latency win on repeated agent loops) but unavoidable. Mitigation: tell users in the binder doc that Claude Code feels slower on FreeRide than on direct Anthropic.

---

## 4. Prior art

### 4.1 claude-code-router (musistudio)

- Repo: `https://github.com/musistudio/claude-code-router`
- 33.7k stars, 790 issues, 131 PRs — very active.
- **License: MIT** (verified).
- Written in TypeScript/Node.
- Architecture: runs as a local server (default `http://127.0.0.1:3456`), user runs `eval "$(ccr activate)"` to set `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`.
- Provider plugins called "transformers": built-ins include `anthropic`, `deepseek`, `gemini`, `openrouter`, `groq`, `maxtoken`, `tooluse`, `reasoning`, `enhancetool`.
- Routing config: per-scenario (`default`, `background`, `think`, `longContext`, `webSearch`) — picks a different model per use-case, similar to FreeRide's auto-routing concept but explicit.

**Should we fork?** No. Three reasons:
1. Different language (Node) — would split FreeRide's mental model.
2. Different scope — claude-code-router is "BYO API keys + route to one provider per scenario". FreeRide is "pool of free-tier providers with cross-provider failover and per-key cooldown". The smart-routing primitive is fundamentally different.
3. We'd inherit ~800 open issues and the maintenance burden of upstream sync.

**Should we use as reference?** **Yes, heavily.** Their `transformers/anthropic.ts` and `transformers/openrouter.ts` are the two files to read most carefully — they have already debugged the streaming tool-use edge cases. Their issue tracker is also a free QA suite for "what breaks Claude Code" (look for issues tagged `streaming`, `tool-use`, `claude-code-bug`).

### 4.2 LiteLLM

- Repo: `https://github.com/BerriAI/litellm`
- License: MIT.
- Anthropic translation lives in `litellm/llms/anthropic/chat/transformation.py` (class `AnthropicConfig` extending `BaseConfig`).
- Key functions: `convert_tool_use_to_openai_format`, `_map_tools`, `_map_tool_choice`, `filter_anthropic_output_schema`, `_build_anthropic_tool_name_maps`, `_basic_sanitize_anthropic_tool_name`.
- Streaming logic is in a sibling file (likely `streaming.py` or `handler.py`).

**Vendor or import?** I'd vendor (copy with attribution) selected helper functions — specifically the schema sanitizer (`_basic_sanitize_anthropic_tool_name`) and the tool-choice mapping. These are 50 lines that have already been debugged.

**Don't import litellm as a dep.** Three reasons:
1. **Critical security note from the Anthropic docs:** "LiteLLM PyPI versions 1.82.7 and 1.82.8 were compromised with credential-stealing malware." Anthropic explicitly warns about it. Pulling litellm in as a transitive dep would put FreeRide's reputation on the line for an upstream supply-chain risk we don't control.
2. litellm is huge (Python wheel is ~10MB+ with all providers). FreeRide is currently ~150KB — a 60x bloat for one feature would betray the "local, lightweight" promise.
3. litellm's translation has ergonomic quirks (e.g. it normalizes responses through its own `ModelResponse` shape, not OpenAI's directly) that we'd have to work around.

**The LiteLLM warning** is also a *positioning opportunity for FreeRide*: "we don't pull untrusted PyPI deps, the gateway is 200KB" is a real differentiator post-Feb-2026.

### 4.3 claude-code-proxy (fuergaosi233)

- Repo: `https://github.com/fuergaosi233/claude-code-proxy`
- License: MIT, Python 99.7%.
- Direct Anthropic Messages → OpenAI translation — exactly the layer we need.
- Supports streaming, tool use, vision (base64).
- Routes Claude haiku/sonnet/opus to configurable models via `SMALL_MODEL`, `MIDDLE_MODEL`, `BIG_MODEL` env vars.

**This is the closest reference implementation.** Read the full repo before writing a line of `anthropic_translate.py`. Their model alias mapping is the same scheme we want for FreeRide (Claude Code asks for `sonnet`, we map to a free model).

### 4.4 claude-code-ollama-proxy (mattlqx)

- Repo: `https://github.com/mattlqx/claude-code-ollama-proxy`
- Python, uses LiteLLM internally. License unspecified in README (assume MIT-ish; needs verification before borrowing code).
- More limited than `fuergaosi233/claude-code-proxy`. Less interesting for us.

### 4.5 Ollama native Anthropic API support

- Announced 2026-01-16: `https://ollama.com/blog/claude` ("Claude Code with Anthropic API compatibility").
- Ollama v0.14.0+ exposes an Anthropic-compatible endpoint at `http://localhost:11434/api/anthropic`-ish.
- Supports streaming, system prompts, tool calling, extended thinking, vision.

**Implication for FreeRide:** Ollama already does what we'd be building, but only for the Ollama provider. FreeRide's value is doing it across **6 providers with failover**. Ollama is now a competitor for the "Claude Code on local LLM" narrative, but FreeRide's "Claude Code on free cloud LLM with auto-failover" is an open lane.

Also — we can study Ollama's implementation as a second reference (Go source, MIT-licensed). When Ollama gets a translation right, we should emulate it.

### 4.6 Cline / Roo Cline

- Both are agent IDEs, not gateways. They speak both OpenAI and Anthropic on the **client** side and pick provider per session. They don't help us — different layer.

### 4.7 Anthropic-published translation layers

- None. Anthropic publishes SDKs (Python, TypeScript, etc.) for the Messages API, but no Messages-from-OpenAI translation. This is fine — translation is a third-party concern by design.

---

## 5. Architectural choices for FreeRide

### Option A — separate subprocess (`freeride-anthropic-shim`)

A second binary listening on `:11344` that translates and proxies to the main FreeRide on `:11343`.

| Pros | Cons |
|---|---|
| Clean isolation — bugs in the shim can't break the OpenAI gateway. | Two processes to babysit. |
| Could be written in any language (e.g. Go for speed). | Provider plugins / health cache / cooldown / events are duplicated or proxied — most of FreeRide's value is in `core/` and we'd lose it through an HTTP boundary. |
| Easier to ship as a separate package. | Extra hop adds latency (~5-15ms per request, more on streaming). |

### Option B — second route on the existing FastAPI app (`/v1/messages`)

Add `freeride/server/routes/messages.py` alongside `chat.py`. Translate to internal `ChatRequest` and reuse the entire failover chain in `chat.py`'s helpers.

| Pros | Cons |
|---|---|
| **Reuses everything**: provider plugins, cooldown, health cache, smart-routing, events, telemetry. | Couples the two routes — a translator bug could (in theory) crash the OpenAI route. Mitigate with strict per-route exception handling (FastAPI does this anyway). |
| Single binary, single process, single config story. | Code surface grows. New tests needed. |
| `/v1/messages` and `/v1/chat/completions` share a request-id namespace — `freeride watch` shows both in the same trace stream. | Ties Anthropic-API support to the FastAPI app's release cadence. (Acceptable — it's the same release cadence.) |

### Option C — separate package (`freeride-anthropic`)

PyPI package that depends on `freeride-gateway` and registers a `freeride.register_anthropic_route()` plugin (assuming we add a plugin system).

| Pros | Cons |
|---|---|
| Optional install — users who don't want it don't pay. | We don't have a plugin system yet; building one for this is yak-shaving. |
| Cleanest separation of concerns. | Doubles the release matrix. |

### Recommendation: **Option B.**

Justification:
1. FreeRide's whole reason to exist is **shared cross-provider failover with health-aware routing**. Every architecture that puts an HTTP boundary between Claude Code and that core surrenders the value prop.
2. The translation is ~1500 lines of pure functions (`anthropic_translate.py`) — adding it to the existing app is small, mechanically. We don't need process isolation for 1500 lines.
3. Single binary matches the marketing site's "one install" promise.
4. Sharing the failover and event log is genuinely useful: when Claude Code fails on Cerebras, the fix lives next to where it surfaces.

### File layout (Option B)

New files:
```
freeride/core/anthropic_translate.py     ~700 LOC. Pure functions:
                                           anthropic_request_to_openai(...)
                                           openai_response_to_anthropic(...)
                                           openai_stream_to_anthropic_sse(...)
                                           anthropic_tool_to_openai_function(...)
                                           openai_tool_call_to_anthropic_block(...)
                                           map_stop_reason(...)
                                           map_usage(...)
freeride/core/anthropic_schema.py        ~300 LOC. Pydantic models for the
                                           Messages request/response shapes
                                           (mirrors core/chat_schema.py).
freeride/server/routes/messages.py       ~400 LOC. FastAPI route handler.
                                           Translates request → reuses chat.py
                                           internals (or its FailoverContext) →
                                           translates response. Streaming version
                                           wraps OpenAI SSE iterator with the
                                           translator.
freeride/binders/claude_code.py          ~150 LOC. Writes Claude Code's
                                           `~/.claude/settings.json` env block:
                                             ANTHROPIC_BASE_URL=http://localhost:11343
                                             ANTHROPIC_AUTH_TOKEN=freeride-local
                                             ANTHROPIC_MODEL=<curated default>
                                             ANTHROPIC_DEFAULT_OPUS_MODEL=...
                                             ANTHROPIC_DEFAULT_SONNET_MODEL=...
                                             ANTHROPIC_DEFAULT_HAIKU_MODEL=...
                                             CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
                                             CLAUDE_CODE_ATTRIBUTION_HEADER=0
freeride/cli/cmd_bind.py                 (modify) add "claude-code" to dispatcher.
docs/agent-binders.md                    (modify) add Claude Code section.
tests/translate/test_anthropic_translate.py    ~600 LOC. Unit tests.
tests/translate/test_messages_route.py         ~300 LOC. Route tests.
tests/translate/golden/                  Golden Anthropic SSE traces vs
                                           OpenAI fixtures. Captured from
                                           real Claude Code sessions.
tests/conformance/test_anthropic_route.py      ~150 LOC. Smoke against a
                                           live Cerebras backend (gated
                                           behind FREERIDE_E2E like the
                                           existing e2e suite).
```

Modified files:
```
freeride/server/app.py                   register the new router.
freeride/cli/cmd_serve.py                advertise /v1/messages in startup banner.
freeride/cli/cmd_doctor.py               add "Claude Code? (try `freeride bind claude-code`)".
README.md                                top-of-fold "Works with Claude Code".
```

The `chat.py` route stays unchanged. Critically, the messages route does **not** call `chat_completions()` over HTTP — it builds a `ChatRequest` and calls the same helper functions (`_build_stream_response`, `_resolve_provider_chain`, `FailoverContext`). We'll likely need to refactor `chat.py` to expose those helpers cleanly (small, well-scoped — Phase 1.5 prep work).

---

## 6. Quality cliff — the make-or-break product question

This is where most "free Claude Code" projects fail. The translation is a tractable engineering problem; **model quality is the unsolvable one**.

### 6.1 Polyglot benchmark grounding

From `https://aider.chat/docs/leaderboards/` (verified 2026-05-09):

| Model | Polyglot % | Edit format |
|---|---|---|
| GPT-5 (high reasoning) | 88.0 | diff |
| GPT-5 (medium) | 86.7 | diff |
| GPT-5 (low) | 81.3 | diff |
| DeepSeek-V3.2-Exp Reasoner | 74.2 | diff |
| DeepSeek R1 (0528) | 71.4 | diff |
| DeepSeek-V3.2-Exp Chat | 70.2 | diff |
| **Llama-4-Maverick** | **15.6** | whole |
| **Gemma-3-27B** | **4.9** | whole |

(Claude Opus 4.7/4.6 and Sonnet 4.6 not listed in the public Aider leaderboard at this snapshot, but cross-referencing the lmcouncil.ai and morphllm benchmarks puts Opus 4.6 in the 70s-80s%.)

Sister doc reference: `AGENT_DISTRIBUTION_FEASIBILITY.md` (per the task brief) cites GPT-5 88%, Claude Opus 82%, DeepSeek-V3.2 70%, Qwen3-235B 62%, Llama-4-Maverick 16%. The numbers above corroborate the Maverick / Gemma cliff.

### 6.2 What this means for Claude Code specifically

Claude Code's prompts are tuned for Claude Sonnet 4.5/4.6/4.7. Two specific failure modes happen when you point Claude Code at a weaker model:

1. **Edit-format regression.** Claude Code's `Edit` tool expects a specific diff-style output. Models that score "whole format" on the polyglot benchmark (Llama-4-Maverick, Gemma-3-27B) reproduce the entire file instead of a diff — Claude Code can't apply that and the user sees "edit failed" loops.
2. **Tool-call hallucination.** Weak models invent tool names, malformed JSON, or interleave plain text with tool_calls in ways the parser can't handle. We've already seen this in OpenClaw integrations — same families of bugs.

### 6.3 Curated "known-good" preset for Claude Code mode

Recommend hardcoded mapping in `freeride/binders/claude_code.py`:

```python
CLAUDE_CODE_KNOWN_GOOD = {
    # Tier 1 — works well end-to-end (≥70% polyglot, diff format)
    "primary": "deepseek/deepseek-chat-v3.2",          # via OpenRouter free
    "primary_alt": "deepseek/deepseek-r1-distill-llama-70b",  # via Groq
    # Tier 2 — fast iteration, lighter tasks (~60% polyglot, but cheap+fast)
    "fast": "llama-3.3-70b-versatile",                 # via Cerebras or Groq
    # Tier 3 — small/haiku replacement for background tasks
    "background": "llama-3.1-8b-instant",              # via Groq
}

CLAUDE_CODE_BLOCKLIST = {
    # Polyglot < 50% or whole-edit-format only — Claude Code WILL break
    "meta-llama/llama-4-maverick",
    "google/gemma-3-27b-it",
    "google/gemma-3-9b-it",
    # No tool calling support → Claude Code unusable
    "google/gemma-2-*",
    # 8K context is below Claude Code's 32K floor
    "*-8k",
}
```

### 6.4 How to surface this to the user

Three layers:

1. **Default behavior**: `freeride bind claude-code` writes `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-code-recommended/opus`, `ANTHROPIC_DEFAULT_SONNET_MODEL=claude-code-recommended/sonnet`, `ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-code-recommended/haiku`. The gateway resolves these aliases to the curated tier-1 free models. Stable — the user's binding doesn't break when DeepSeek-V3.2 gets retired; only our internal mapping does.
2. **`/model auto`**: Claude Code's `/model auto` (via gateway model discovery) shows the curated list, not the raw catalog. The `display_name` field labels them (e.g. "FreeRide: DeepSeek V3.2 (Sonnet-class, free)"). This needs `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` in our env block.
3. **`freeride doctor` warning**: if the user manually overrode `ANTHROPIC_MODEL` to something on the blocklist, doctor prints a yellow warning: "You're using `meta-llama/llama-4-maverick` with Claude Code. This model scores 15.6% on aider polyglot and produces whole-file rewrites Claude Code can't apply. Recommended: unset `ANTHROPIC_MODEL` or run `freeride bind claude-code` to reset."

### 6.5 The honest user expectation

**FreeRide-on-Claude-Code is "good for Haiku replacement and lightweight tasks; mediocre for serious refactoring; not a substitute for Sonnet 4.6 on hard problems."**

We must say this in the README. If we oversell it, the first impression will be "Claude Code on FreeRide produces broken code" and we won't recover.

---

## 7. ToS / liability

### 7.1 Allowed by Anthropic ToS?

**Yes.**

Evidence:
- `code.claude.com/docs/en/llm-gateway` explicitly documents `ANTHROPIC_BASE_URL` use with non-Anthropic gateways. They walk through LiteLLM, Bedrock, Vertex setups by name.
- Anthropic's own docs say: "When `ANTHROPIC_BASE_URL` points at a gateway that exposes the Anthropic Messages format, Claude Code can query the gateway's `/v1/models` endpoint at startup..." — a feature designed for our exact use case.
- We are **not** using Anthropic API credentials, so the "subscription redistribution" clause in the Commercial Terms doesn't apply (per `https://www.sitepoint.com/end-wrapper-era-anthropic-api-terms-saas/` and forum analysis).
- Anthropic's recent (Feb 2026) restriction was specifically about the **Agent SDK** requiring API key (not OAuth) auth — which doesn't affect Claude Code itself, and doesn't affect us at all since we don't authenticate to Anthropic.

### 7.2 Are we redistributing or modifying Claude Code?

**No.** Three concrete points:
- The user installs Claude Code via `npm install -g @anthropic-ai/claude-code` (or equivalent) themselves, from Anthropic's official channel.
- We don't bundle, repackage, fork, or modify Claude Code's binary or sources.
- `freeride bind claude-code` writes only the env block in `~/.claude/settings.json` — config Anthropic explicitly supports user-editing.

This is the same legal posture as `aider`, `continue`, `claude-code-router`, all of which Anthropic has tolerated for over a year.

### 7.3 Naming / trademark

**"Claude Code" is an Anthropic trademark.** Three concrete safe phrasings:

- ✅ "Works with Claude Code" — descriptive, not implying endorsement.
- ✅ "Use Claude Code with FreeRide" — descriptive use of the product name.
- ✅ `freeride bind claude-code` — fine; it's a config target name, like `freeride bind aider`.
- ❌ "FreeRide for Claude" / "ClaudeRide" / "Claude Free" — implies derivation. Avoid.
- ❌ Using the Claude Code logo or distinctive UI in marketing — avoid.

This matches existing FreeRide binder naming (`aider`, `continue`, `hermes`, `openclaw`). No new policy needed.

### 7.4 Liability for failures

If a Claude Code user runs `freeride bind claude-code` and a free model produces a broken edit:
- The user's repo is unharmed unless Claude Code applied the edit (their decision, not ours).
- Our README must be explicit: "Free-tier models score 60–75% on polyglot benchmarks vs Claude Sonnet's ~85%. Expect more retries and occasional bad diffs. Use Claude Code's review-before-apply prompt mode for production code."
- Our LICENSE (MIT) already disclaims warranty.

The harder concern is **reputation**, not legal liability. Mitigation = curated model preset (§6.3) + honest expectation-setting (§6.5).

---

## 8. Phased build plan

Total estimate: **10–14 engineer-days** + ~2 days QA buffer. Phases sized to be independently shippable.

### Phase 1 — Non-streaming chat (3 days)

**Scope:** `POST /v1/messages` with `stream: false`. Text content blocks only. No tools. No vision. No system prompts (yet — actually do system).

**Files:**
- New: `freeride/core/anthropic_schema.py` (Pydantic models)
- New: `freeride/core/anthropic_translate.py` (request/response only — no streaming, no tools)
- New: `freeride/server/routes/messages.py` (route handler — stream=false path)
- New: `tests/translate/test_anthropic_translate_basic.py` (~30 cases)

**Modify:**
- `freeride/server/app.py` register the router.
- `freeride/server/routes/chat.py` extract `_resolve_provider_chain` and the non-streaming failover loop into a shared helper module (refactor — small).

**Deliverable:** users can `curl http://localhost:11343/v1/messages -d '{"model":"sonnet","messages":[{"role":"user","content":"hi"}],"max_tokens":100}'` and get a valid Anthropic-shaped response. Smoke test against Claude Code with `--print "hi"` works. **Tool use, streaming, vision will fail** — those come next.

### Phase 2 — Streaming (3–4 days)

**Scope:** `stream: true`. Text content blocks. SSE event-by-event translation. `ping` events on schedule.

**Files:**
- Extend `freeride/core/anthropic_translate.py` with `openai_stream_to_anthropic_sse(...)` async generator.
- Extend `freeride/server/routes/messages.py` with the streaming branch.
- New: `tests/translate/test_anthropic_streaming.py` — uses fixture OpenAI streams, asserts the SSE output line-by-line.
- New: golden traces in `tests/translate/golden/`.

**Deliverable:** Claude Code interactive session works for plain Q&A. No tools yet — Claude Code will fail when it tries to call `Read`/`Edit`/etc.

### Phase 3 — Tool use (4–5 days, the gnarly one)

**Scope:** Tool definitions, `tool_use` content blocks, `tool_result` content blocks, `tool_choice`. Both streaming and non-streaming. Full state machine for `input_json_delta` accumulation.

**Files:**
- Extend `freeride/core/anthropic_translate.py` with the four tool functions (request side, response side, request-message tool_result handling, streaming state machine).
- Extend `tests/translate/test_anthropic_translate_basic.py` and `test_anthropic_streaming.py` with tool cases.
- New: `tests/translate/test_tool_use_state_machine.py` — pure-unit tests for the streaming state machine. Worth its own file because this is where the bugs will be.
- New: e2e test against a known tool-calling-capable free model (Cerebras Llama-3.3-70B is the fastest verifier).

**Deliverable:** Claude Code can read files, edit files, run bash. Real coding sessions work. **This is the moment we can dogfood internally.** Spend a full day driving Claude Code against FreeRide on a real refactoring task before declaring Phase 3 done.

### Phase 4 — Quality presets, binder, docs (2–3 days)

**Scope:** `freeride bind claude-code`, model alias mapping, doctor warnings, model discovery endpoint, README + agent-binders.md updates.

**Files:**
- New: `freeride/binders/claude_code.py`
- Modify: `freeride/cli/cmd_bind.py` (add `claude-code` dispatch)
- Modify: `freeride/cli/cmd_doctor.py` (add Claude Code presence check + blocklist warning)
- Modify: `freeride/server/routes/models.py` to surface Anthropic-style model ids when the request comes via `/v1/models?for=anthropic` (or a separate `/v1/anthropic/models` route that filters to Claude-Code-known-good entries with `display_name`).
- New: `tests/binders/test_claude_code_binder.py`
- Modify: `docs/agent-binders.md`, `README.md`, `internal-docs/PROJECT_STATE.md` (binder catalog table).

**Deliverable:** `pip install freeride-gateway && freeride bind claude-code && claude` Just Works. README hero gains "Works with Claude Code" badge.

### Phase 5 (deferred) — vision, thinking, count_tokens, edge cases

Vision is provider-conditional. Thinking is mostly drop. `count_tokens` we can implement as a simple character-based heuristic (Claude Code uses it for context-window display only, not for routing decisions, per the SDK source).

**Effort:** 2 days. Ship as a follow-up release after Phase 4 has been in the wild for ~2 weeks and we know what users actually hit.

### Effort table

| Phase | Days | Cumulative |
|---|---|---|
| 1: Non-streaming chat | 3 | 3 |
| 2: Streaming | 3–4 | 6–7 |
| 3: Tool use | 4–5 | 10–12 |
| 4: Binder + docs + presets | 2–3 | 12–15 |
| QA buffer (golden traces, real Claude Code dogfood) | 2 | **14–17** |
| Phase 5 (vision/thinking — deferred) | 2 | 16–19 |

For a shipping target: **call it 2–3 calendar weeks of focused effort, including dogfooding.** If we go faster, it's because we lifted heavily from `claude-code-proxy` (allowed under MIT with attribution).

---

## 9. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Streaming tool-use translation bugs cause Claude Code to freeze mid-edit. | **High** | Golden-trace test fixtures recorded from real Claude Code sessions. Fuzz the partial-JSON accumulator. Read `claude-code-proxy` source before writing our own. Always emit `content_block_stop` + `message_delta` + `message_stop` even on upstream error. |
| R2 | Free-tier model quality (Llama-4-Maverick at 15.6%, Gemma at 4.9% polyglot) ruins first impression. | **High** | Curated known-good preset (§6.3). Doctor warning if user pins a blocklist model. Honest README expectation-setting. Default to DeepSeek V3.2 / Llama 3.3 70B which actually work. |
| R3 | Context-window mismatch — Claude Code expects 200K, free models give 8K-128K, silent truncation produces broken edits. | **Medium** | Reject in `messages.py` if the request's estimated token count exceeds the resolved model's context window. Surface a clear 400. Add a doctor check: "Your selected model has 32K context; Claude Code recommends ≥64K." |
| R4 | `cache_control` loss makes free-tier feel slow vs direct Anthropic (no 90% cache discount). | **Medium** | Document the gap honestly in the binder doc. Use `CLAUDE_CODE_ATTRIBUTION_HEADER=0` to at least let providers' native caching (if any) kick in. Long-term: implement a FreeRide-side prompt cache keyed on prefix hash (Phase 6+). |
| R5 | Llama / Qwen produce malformed tool_call JSON. Claude Code parses it as an empty tool call and errors. | **Medium** | In the translator, if `tool_call.function.arguments` is malformed JSON, emit `input: {}` and an extra text block "[tool_call argument parse error: <msg>]". Don't 500. Log to events.jsonl for the watcher. |
| R6 | Mid-stream upstream errors after the first chunk has shipped — Claude Code doesn't see them as Anthropic-shaped errors. | **Medium** | Always emit a synthetic `content_block_stop` + `message_delta{stop_reason:"end_turn"}` + `message_stop` on upstream failure. Better to truncate cleanly than freeze. (FreeRide's existing buffer-first-chunk failover handles pre-first-chunk errors.) |
| R7 | Anthropic changes the Messages spec or adds beta features Claude Code starts using. | **Low** | Set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in our binder to opt out of betas. Watch the `code.claude.com/docs` changelog. The non-beta spec has been stable since 2023-06-01 (`anthropic-version: 2023-06-01`). |
| R8 | LiteLLM-style supply-chain attack via vendored translation code. | **Low** | We're not depending on litellm. If we lift specific helper functions, copy them in directly with attribution; review each line. (Anthropic's docs explicitly warn about LiteLLM's 1.82.7/1.82.8 malware incident — same trap waits for us if we add untrusted deps.) |
| R9 | Anthropic ToS shifts to disallow non-Anthropic backends. | **Low** | Unlikely — the feature is too useful internally to enterprise. If it happens, we revert to the OpenAI-format gateway only and lose the Claude Code surface but keep everything else. |
| R10 | Quality drop ruins user's first impression of FreeRide overall (not just Claude Code). | **Medium** | Phase 4 ships as **technical preview** — readme + announcement post both say "alpha quality, free-tier free Claude Code, expect rough edges". Ship after Phases 1–3 plus 2 weeks of dogfooding. Don't make Claude Code the headline of any release. |
| R11 | `claude-code-router` gets there first / better and we look like a copy. | **Medium** | We don't compete on the same axis: ccr is "BYO API key, route to one provider per scenario". FreeRide is "free-tier pool with cross-provider failover". Be loud about that distinction in the README hero. |
| R12 | Token-count usage drift (Anthropic's tokenizer ≠ OpenAI provider's tokenizer) causes Claude Code's context-meter to under/overcount. | **Low** | Pass through whatever the provider returns. Note in docs that the usage numbers are approximate. |

---

## 10. Recommendation

### Verdict: **Yes, with caveats. Build it, but ship it as Tech Preview and be honest about the quality tradeoff.**

**Why yes:**
1. The translation is a solved problem (claude-code-proxy, claude-code-router, LiteLLM all do it). We can lift heavily from MIT-licensed prior art.
2. Anthropic supports the use case explicitly via documented env vars.
3. Claude Code is the dominant agentic coding CLI. Even a 10% conversion rate on Claude Code users adopting FreeRide is a category-defining moment for FreeRide.
4. The technical work fits cleanly into FreeRide's existing architecture (Option B, ~2000 LOC across 4 new files). No new processes, no new languages, no new infrastructure.

**The caveats:**
1. **Curated model preset is non-negotiable.** Without §6.3, Claude Code on FreeRide produces broken edits and the project gets a reputation for "free but broken." With it, FreeRide on DeepSeek V3.2 is genuinely useful for many real coding workflows.
2. **Phase 4 is the quality bar, not Phase 3.** Don't announce "Claude Code support" until the binder writes the curated preset, the doctor warning is in place, and we've dogfooded a real refactoring session.
3. **Position honestly.** "Claude Code on free tier — works for routine refactors, lightweight tasks, prototyping. For serious production code, use real Claude Sonnet 4.6." Anything more aspirational gets us bitten.

### Order of operations vs. roadmap

This goes **after** the next two items already in flight (per `PROJECT_STATE.md`):
1. ~Cerebras provider~ (already in tree as `freeride/providers/cerebras.py` — verify production-ready)
2. ~Ollama provider~ (already in tree as `freeride/providers/ollama.py`)

This goes **before** these (in priority order):
- `freeride install <agent>` (Claude Code proves the agent-distribution thesis is worth pursuing — do this first)
- chat demo on the marketing site (use Claude Code traces as the live example)
- `freeride.register()` Python plugin API (more general; Claude Code is more specific and shippable)

`MULTI_PROVIDER_SIGNUP_FEASIBILITY.md` (sister doc) is unrelated — a different axis (key onboarding) — and doesn't block this work.

### First concrete next step

**Do not start coding. Spec out Phase 1 in a separate 2-page design doc that:**
1. Defines the exact Pydantic shape of `AnthropicMessageRequest` and `AnthropicMessageResponse`.
2. Shows the field-by-field translation for the simple "hello" non-streaming case as a runnable Python snippet.
3. Specifies the test fixtures: capture three real Claude Code requests against `api.anthropic.com` (using `mitmproxy` as the user, while logged in to Claude Code with their own Anthropic key) and three corresponding OpenAI calls. Save them as JSON. These become the golden inputs for `test_anthropic_translate_basic.py`.
4. Gets Shaiv's sign-off on the route layout (Option B, file paths in §5) before any code lands.

Once the spec is signed off, Phase 1 should land in 3 working days. Phase 2 in another 3–4. By then we'll know whether the gnarly Phase 3 estimate holds.

---

## Appendix A — Source URLs (cited)

**Anthropic & Claude Code:**
- LLM gateway config: https://code.claude.com/docs/en/llm-gateway
- Env vars: https://code.claude.com/docs/en/env-vars
- Model config: https://code.claude.com/docs/en/model-config
- Messages API spec: https://platform.claude.com/docs/en/api/messages
- Messages streaming spec: https://platform.claude.com/docs/en/api/messages-streaming
- Models overview: https://platform.claude.com/docs/en/about-claude/models/overview

**Prior art:**
- claude-code-router (musistudio): https://github.com/musistudio/claude-code-router (MIT, 33.7k★)
- claude-code-proxy (fuergaosi233): https://github.com/fuergaosi233/claude-code-proxy (MIT, Python)
- claude-code-ollama-proxy (mattlqx): https://github.com/mattlqx/claude-code-ollama-proxy
- LiteLLM Anthropic translation: https://github.com/BerriAI/litellm/tree/main/litellm/llms/anthropic
- LiteLLM compromised versions advisory: https://github.com/BerriAI/litellm/issues/24518
- Ollama Anthropic API support announcement (2026-01-16): https://ollama.com/blog/claude
- Ollama integration docs: https://docs.ollama.com/integrations/claude-code

**Benchmarks:**
- Aider polyglot leaderboard: https://aider.chat/docs/leaderboards/
- LM Council benchmarks (May 2026): https://lmcouncil.ai/benchmarks
- AI coding benchmarks 2026 review: https://www.morphllm.com/ai-coding-benchmarks-2026

**ToS & licensing:**
- Anthropic API terms analysis: https://www.sitepoint.com/end-wrapper-era-anthropic-api-terms-saas/
- claude-mem custom-backends doc (community): https://docs.claude-mem.ai/configuration/custom-anthropic-backends
- Coder LLM gateway client config: https://coder.com/docs/ai-coder/ai-gateway/clients
- Renezander local-LLM guide: https://renezander.com/guides/claude-code-local-llm-anthropic-base-url/

**FreeRide internal references:**
- `freeride-web/internal-docs/PROJECT_STATE.md` (project layout, binder catalog, provider catalog)
- `FreeRideV3/freeride/server/routes/chat.py` (existing OpenAI route — failover patterns to reuse)
- `FreeRideV3/freeride/v2compat/openclaw.py` (precedent: shim for an Anthropic-format-ish agent)
- `FreeRideV3/freeride/binders/{aider,continue_,hermes}.py` (binder pattern to follow)
