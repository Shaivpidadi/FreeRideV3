# Translators

> How the gateway accepts three different agent-CLI wire protocols (Anthropic, Google, OpenAI Responses) and serves them all out of the same provider chain.

## Why this exists

The major coding CLIs each speak their vendor's native protocol:

| CLI | Endpoint | Request shape | Streaming format |
|---|---|---|---|
| Claude Code | `/v1/messages` | Anthropic Messages — `{messages, system, tools, …}` | Anthropic SSE — `message_start` → `content_block_*` → `message_stop` |
| Codex | `/v1/responses` | OpenAI Responses — `{input, instructions, tools, …}` with typed input items | Responses SSE — `response.created` → `response.output_item.added` → `response.output_text.delta` → `response.completed` |
| Gemini CLI | `/v1beta/models/<m>:generateContent[+stream]` | Google — `{contents, systemInstruction, tools.functionDeclarations, generationConfig}` | Google SSE — each frame is a complete-shape response with partial content |

Under the hood, our routing layer speaks OpenAI Chat Completions — the canonical lowest-common-denominator that every free-tier provider supports. So each CLI's protocol needs to be translated *to* Chat Completions on the request, and *back from* Chat Completions on the response. Streaming events have to be re-framed in the CLI's expected protocol so SDKs parse them natively.

```
                              ┌────────────────────────────┐
   POST /v1/messages    ─────▶│  anthropic_translate.py    │
   (Claude Code)              │  Messages ↔ ChatCompletions│
                              └─────────────┬──────────────┘
                                            │
                              ┌────────────────────────────┐
   POST /v1/responses   ─────▶│  codex_translate.py        │
   (Codex)                    │  Responses ↔ ChatCompletions
                              └─────────────┬──────────────┘
                                            │
                              ┌────────────────────────────┐
   POST /v1beta/models/ ─────▶│  gemini_translate.py       │
   (Gemini CLI)               │  GoogleAI ↔ ChatCompletions│
                              └─────────────┬──────────────┘
                                            │
                                            ▼
                            ┌────────────────────────────┐
                            │  Chat Completions failover │
                            │  → openrouter / groq / … │
                            └────────────────────────────┘
```

This doc covers the design choices that recur across all three. For protocol-specific details see the source files cited at the bottom.

## Schema strategy: permissive in, strict out

Every schema is declared with `model_config = ConfigDict(extra="allow")`. The reason: each vendor adds fields to their wire format quietly, and we'd rather pass an unknown field through than 400 a request because the user upgraded their CLI. The cost is small — Pydantic accepts the extras into `model_extra`, the translator ignores what it doesn't understand, and we never have to chase down "they added a new field" bugs.

On the *outbound* (response) side, we serialize back via `model_dump(by_alias=True, exclude_none=True)` so:
- camelCase aliases (Google's `systemInstruction` from snake_case `system_instruction`) round-trip cleanly
- fields we didn't populate aren't sent (avoids `"foo": null` surprising the client)

## Tool definitions: three shapes

Each protocol structures tool defs differently:

```python
# Anthropic / OpenAI Chat Completions — wrapped in {type: function, function: {...}}
{"type": "function",
 "function": {"name": "Write", "description": "...", "parameters": {...}}}

# OpenAI Responses — FLAT, no nested 'function' key
{"type": "function",
 "name": "Write", "description": "...", "parameters": {...}}

# Google — nested under functionDeclarations[]
{"functionDeclarations": [
    {"name": "Write", "description": "...", "parameters": {...}}
]}
```

The translators are responsible for the (un)wrapping. Codex's Responses-shape tools also include built-in types (`web_search`, `custom`, `mcp`, `file_search`, `code_interpreter`) which free providers don't accept — the codex translator filters those out before reaching upstream. Function tools forward; everything else drops silently.

## Tool results: even more divergent

| Protocol | How a tool result travels back to the model |
|---|---|
| Chat Completions | `{"role": "tool", "tool_call_id": "...", "content": "..."}` in messages |
| Anthropic Messages | A user-role message with `{"type": "tool_result", "tool_use_id": "...", "content": "..."}` content block |
| OpenAI Responses | An `input` item: `{"type": "function_call_output", "call_id": "...", "output": "..."}` (note: `call_id`, not `tool_call_id`) |
| Google | Newer: user-role message with `{"functionResponse": {"name": "...", "response": {...}}}` part. Older: `role: "function"` with the same part. |

The translators normalize all four to OpenAI's `role: "tool"` on the way in. On the way out, the response side never *emits* a tool_result (only the model produces tool_call requests; the caller's next turn provides the results). So the translation is request-side only for tool results.

## Streaming: re-framing chunks into vendor-specific event protocols

This is the deepest part. Chat Completions streams `delta`-shape chunks:

```
data: {"choices":[{"delta":{"content":"Hello"}}]}
data: {"choices":[{"delta":{"content":" world"}}]}
data: {"choices":[{"finish_reason":"stop"}]}
```

Each vendor's SDK expects a *different* event protocol around / instead of this:

### Anthropic SSE

Each event has both `event:` and `data:` lines. Sequence:

```
event: message_start                  data: {message: {id, role, …}, usage: {…}}
event: content_block_start            data: {index: 0, content_block: {type: "text", text: ""}}
event: content_block_delta            data: {index: 0, delta: {type: "text_delta", text: "Hello"}}
event: content_block_delta            data: {index: 0, delta: {type: "text_delta", text: " world"}}
event: content_block_stop             data: {index: 0}
event: message_delta                  data: {delta: {stop_reason: "end_turn"}, usage: {…}}
event: message_stop                   data: {}
```

For tool_use blocks: `content_block_start` carries `{type: "tool_use", id, name, input: {}}`, deltas carry `{type: "input_json_delta", partial_json: "…"}`. Translator state machine tracks `current_index` (monotonic) and `current_kind` (text / tool_use / thinking) to emit the right framing.

### Google AI SSE

"Incremental complete responses" — each chunk is a full GeminiGenerateResponse-shaped object with **partial** content:

```
data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hello"}]},"index":0}],"modelVersion":"gemini-2.0-flash"}
data: {"candidates":[{"content":{"role":"model","parts":[{"text":" world"}]},"index":0}]}
data: {"candidates":[{"content":{"role":"model","parts":[]},"finishReason":"STOP"}],"usageMetadata":{…}}
```

Text deltas → one chunk per delta with text in `parts[0].text`. Tool calls don't stream incrementally in Google's protocol — the SDK expects them whole — so the translator buffers tool_call args until the JSON parses cleanly, then emits ONE chunk with the complete `functionCall`.

### OpenAI Responses SSE

The most ceremonious. Each event has both `event:` and `data:` lines, every event payload has a `sequence_number` field, and clients **gate on framing events** (`output_item.added` / `content_part.added`) before consuming deltas (see litellm #22102 for what breaks when you skip them).

Order:

```
response.created                      response object, status: "in_progress"
response.in_progress                  same object
response.output_item.added            output_index: 0, item: {type: "message", id, status: "in_progress", content: []}
response.content_part.added           item_id, output_index, content_index: 0, part: {type: "output_text", text: ""}
response.output_text.delta            item_id, output_index, content_index, delta: "Hello"
response.output_text.delta            item_id, output_index, content_index, delta: " world"
response.output_text.done             item_id, output_index, content_index, text: "Hello world"
response.content_part.done            (mirror of added)
response.output_item.done             (mirror of added, item.status: "completed", item.content populated)
response.completed                    final response object with usage
```

Function calls take the `function_call_arguments.delta` / `.done` branch instead of `output_text`, but the outer `output_item.added` / `output_item.done` framing is identical.

`codex_translate._StreamFramer` is the state machine. It tracks `current_kind` (`None` / `text` / `function_call:<oai_index>`), `output_index` (monotonic), `sequence_number` (monotonic across all events), and per-tool-call buffers. Transitions close the current item with its terminator events before opening the next.

## Finish reasons: lossy mapping

OpenAI's `finish_reason` is narrower than the protocols we translate to. The mappings:

| OpenAI | Anthropic | Google | OpenAI Responses |
|---|---|---|---|
| `stop` | `end_turn` | `STOP` | status `completed` |
| `length` | `max_tokens` | `MAX_TOKENS` | status `incomplete` + `incomplete_details.reason: "max_output_tokens"` |
| `tool_calls` | `tool_use` | `TOOL_CALL` | status `completed` (tool_call lives in output[], not a status flag) |
| `content_filter` | `stop_sequence` | `SAFETY` | status `incomplete` + `incomplete_details.reason: "content_filter"` |

The Responses protocol's "tool_calls is still completed status" is a useful subtle point — clients shouldn't treat tool calls as a degraded state.

## Usage rollup

| Protocol | Token field names |
|---|---|
| Chat Completions | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| Anthropic | `input_tokens`, `output_tokens` |
| Google | `promptTokenCount`, `candidatesTokenCount`, `totalTokenCount` |
| OpenAI Responses | `input_tokens`, `output_tokens`, `total_tokens` (plus `input_tokens_details`, `output_tokens_details`) |

Trivial mappings, but each protocol expects its own field names — clients break otherwise.

## Model id echo: requested vs resolved

Every translator echoes the **originally-requested** model id on the response, not the one we actually routed to upstream. So a Claude Code user typing `/model freeride/coding` sees `freeride/coding` come back even though the actual answer came from `openrouter/owl-alpha`. Same for `gemini-2.5-pro` → `openrouter/owl-alpha`, `gpt-5-codex` → `openrouter/owl-alpha`.

The real provider is exposed via the `X-FreeRide-Provider` response header for debugging. The body's model field stays cosmetic to keep the CLI UIs coherent.

## Why three translators instead of one

We considered a single "universal translator" that maps everything through an intermediate canonical form. Rejected because:

1. Each protocol has features the others don't (Anthropic's `thinking` blocks, Codex's typed-input-items, Google's multimodal parts). A universal canonical would either be a superset (carrying baggage for every feature) or lossy (silently dropping protocol-specific details).
2. Streaming protocols are deeply different. The Anthropic state machine tracks `content_block` indices; the Responses one tracks `output_item` AND `content_part` indices AND `sequence_number`; Google emits complete-shape chunks. A universal streamer would be 3x bigger than three focused ones.
3. Each translator gets its own permissive schema and forward-compat strategy — when a vendor adds a field, only that translator changes.

The cost is duplication (similar patterns repeated three times — finish-reason mapping, usage rollup, model-echo). The benefit is clarity: each translator is one file, one mental model, one set of tests.

## Source map

| Protocol | Schema | Translator | Route |
|---|---|---|---|
| Anthropic | `freeride/core/anthropic_schema.py` | `freeride/core/anthropic_translate.py` | `freeride/server/routes/messages.py` |
| OpenAI Responses | `freeride/core/codex_schema.py` | `freeride/core/codex_translate.py` | `freeride/server/routes/codex.py` |
| Google AI | `freeride/core/gemini_schema.py` | `freeride/core/gemini_translate.py` | `freeride/server/routes/gemini.py` |

Each translator has a sibling test file in `tests/` covering both directions plus the streaming state machine. Mock-stream chunks in, parse SSE bytes out, assert against the expected protocol.
