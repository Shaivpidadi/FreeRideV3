# NVIDIA NIM — Provider Reference

> Reference for FreeRide V3's NIM provider plugin. Built from the public docs
> (sparse) plus direct live probes against `https://integrate.api.nvidia.com`
> using a working `nvapi-` key on 2026-05-07. Where docs are silent, the source
> is labelled **(probe)**.

---

## Protocol implications (read first)

Three quirks force decisions in the FreeRide Provider Protocol (the design plan):

1. **Auth failures are HTTP 403, not 401.** Bad token returns
   `403 Forbidden` with `application/problem+json`:
   `{"status":403,"title":"Forbidden","detail":"Authorization failed"}` (probe).
   `classify_error` must map both 401 and 403 → `ErrorKind.AUTH` for NIM.

2. **MODEL_NOT_FOUND is a routing-layer reject, not a JSON error.**
   Sending an unknown model id (`unknownco/some-model` or
   `meta/llama-3.1-99b-instruct`) returns `HTTP 404` with a plain-text body
   `404 page not found` — no JSON envelope (probe). `classify_error` for NIM
   needs to treat 404 + non-JSON body as `MODEL_NOT_FOUND`.

3. **No programmatic free-tier discrimination.** Every model in `/v1/models`
   is callable by a personal-account key. There is no `is_free` or `pricing`
   field. Free credits are a single account-level pool; depletion fails *all*
   models, not just paid ones. The Provider Protocol's
   `list_free_models(key)` for NIM must therefore either (a) return everything
   in the catalog and trust the resolver to react to errors, or (b) maintain a
   curated allowlist of "tier 1" personal-credit-friendly models. Recommend
   (b) — match the v2 design intent — with a small static list (Llama 3.1/3.2/3.3,
   DeepSeek, Mistral, Qwen, Gemma) plus an env override for power users.

Other deltas listed below are within the protocol's "providers adapt where
their API differs" allowance.

---

## Auth

- **Header:** `Authorization: Bearer <key>` (probe; matches OpenAI conv.).
- **Key format:** `nvapi-<base64ish-string>`, ~64 chars (observed).
  Generated at https://build.nvidia.com/ → "Get API Key".
- **Key lifecycle:** keys do not expire by default; revocable from the
  build.nvidia.com console. (https://docs.nvidia.com/nim/index.html — overview;
  exact lifecycle policies not documented publicly.)
- **Multi-key:** the Bearer token is per-key; nothing in the API prevents
  multi-key rotation client-side. FreeRide's `KeyCooldown` works unchanged.

## Catalog

- **Endpoint:** `GET https://integrate.api.nvidia.com/v1/models`.
- **Response shape:** OpenAI-compatible list:
  ```json
  {"object":"list","data":[
    {"id":"meta/llama-3.1-8b-instruct","object":"model",
     "created":735790403,"owned_by":"meta"}
  ]}
  ```
- **Field set is minimal** (probe): only `id`, `object`, `created`, `owned_by`.
  **No** `context_length`, `pricing`, `modalities`, `supported_parameters`.
- **Catalog has duplicates.** `deepseek-ai/deepseek-v4-flash` and
  `deepseek-ai/deepseek-v4-pro` each appear twice with identical fields
  (probe, 2026-05-07, 136 entries with 2 dupes). FreeRide must dedupe by `id`
  when ingesting.
- **No server-side filter.** `?owner=meta&limit=5` is silently ignored;
  full list returned every time (probe). FreeRide must filter client-side.
- **Per-model metadata** lives only on the build.nvidia.com model-card pages
  (https://build.nvidia.com/<owner>/<model>) which are HTML, not API. Practical
  consequence: for v3.0, ship a hand-curated `nim_model_metadata.py` that maps
  api_id → context length, modalities, capability flags. Refresh manually.

## Free-tier detection

There is no API signal. NVIDIA's pricing page describes a "Personal" tier
with free credits (count not numerically documented in the public pages we
fetched 2026-05-07; community reports place initial allotment at ~5,000
credits). All catalog models are callable until the personal credit pool is
exhausted, after which calls fail.

**FreeRide approach:** maintain `providers/nim_free_models.py` —
hardcoded curated list of models known to work well on the personal tier.
Allow `NVIDIA_NIM_FREE_MODELS_OVERRIDE` env var as JSON array for power users
who hit different working sets.

## Credit accounting

Probed `GET /v1/credits`, `/v1/usage`, `/v1/account`, `/v1/me`, `/credits`,
`/usage`, `/account` — **all 404** (probe). No public endpoint for remaining
credits. Users must check https://build.nvidia.com/.

Implication for FreeRide: `freeride status` cannot show a remaining-credits
number for NIM. Show "unknown" with a link to the dashboard. Document this
as a known limitation in §13 D13's quota-visibility section.

## Probe convention

- Standard `max_tokens: 5` chat-completion against the target model works
  (probe — `meta/llama-3.1-8b-instruct` returned 5 tokens, billed 36 prompt +
  5 completion = 41 against the credit pool).
- **Probe billing:** every probe consumes credits. With curated free model
  list of ~6 models and probe budget of "top-N=5" per probe loop, ~205 tokens
  per loop × 4 loops/hour = ~820 tokens/hour. Negligible against a 5,000-credit
  personal pool.
- No model in the catalog rejected the standard probe shape during testing.
  Vision models accept `[{"type":"text"}]`-only content (probe — single text
  message to llama-3.2-11b-vision-instruct succeeded).

## Error classification

| Status | Body | Classify as | Notes |
|---|---|---|---|
| 200 | JSON success | `OK` | |
| 403 | `{"status":403,"title":"Forbidden","detail":"Authorization failed"}` (`application/problem+json`) | `AUTH` | Probe with bogus key. NIM uses 403, not 401. |
| 404 | `404 page not found` (text/plain) | `MODEL_NOT_FOUND` | Probe with bogus model id. Plain-text body is the tell. |
| 500 | `failed to decode json body: ...` (text/plain) | upstream BAD_REQUEST treat as `UNKNOWN`/`UNAVAILABLE` | Probe with malformed JSON. NIM's gateway returns 500 for parse errors — don't assume 500 is server-side. |
| 429 | not directly probed; expected JSON shape per OpenAI conv. | `RATE_LIMIT` | NIM uses request-rate limits per personal tier; not numerically documented. |
| 402 | not probed | likely `QUOTA_EXHAUSTED` | Standard payment-required code; assume credits-exhausted maps here. **Verify in production**. |
| 5xx | varies | `UNAVAILABLE` | Standard transient. |

**`retry_after_hint`:** No `Retry-After` header observed on 200 responses
(probe — only `nvcf-reqid`, `nvcf-status`, `access-control-expose-headers`,
`vary`, `date`, `content-type`, `content-length`). Behavior on 429 not
verified. FreeRide should still extract `Retry-After` if present, fallback
to a fixed 60s hint otherwise.

## Streaming

- **Format:** standard SSE. Each event is `data: {...}\n\n` (probe).
- **Termination:** `data: [DONE]\n\n` (probe).
- **Penultimate event** has `choices: []` and includes `usage`
  totals: `{"prompt_tokens":36,"completion_tokens":3,"total_tokens":39,
  "prompt_tokens_details":{"cached_tokens":32}}` (probe). FreeRide's stats
  collector must read tokens from that event, not the [DONE] sentinel.
- **Per-chunk shape** is OpenAI-compatible:
  `{"id":"...","choices":[{"index":0,"delta":{"content":"x","role":"assistant"}}],
  "created":...,"model":"...","object":"chat.completion.chunk"}` plus an
  `nvext` field on each chunk (worker_id and timing breakdowns) — the
  `nvext` is non-standard and FreeRide should strip it before forwarding to
  clients to keep wire-clean OpenAI compat.

## Capabilities (tools, vision, structured outputs)

All probed live, all work on at least one tested model:

- **Tool calls:** `meta/llama-3.1-70b-instruct` (probe). Request shape
  identical to OpenAI; response includes
  `tool_calls: [{"id":"chatcmpl-tool-...","type":"function","function":{"name":..., "arguments":...}}]`,
  `finish_reason: "tool_calls"`. Also includes a vLLM-style `stop_reason: 128008`
  (token-id) extension — non-standard, strip before forwarding.
- **Structured outputs (`response_format: {"type":"json_object"}`):**
  `meta/llama-3.1-70b-instruct` (probe). Returns valid JSON in `content`.
  `response_format: {"type":"json_schema", ...}` — not probed; assume same.
- **Vision:** `meta/llama-3.2-11b-vision-instruct` (probe). Accepts `content: [{type:"text"},{type:"image_url",image_url:{url:"data:image/png;base64,..."}}]`.
  **Vision tokens are heavy** — a 1×1 transparent PNG was 1,613 prompt tokens.
  Cost-conscious users should be warned.
- **Logprobs:** not probed. Most NIM-served models do not expose them.

**Capability detection:** the catalog gives no signal. FreeRide must
hardcode the cap-flag matrix in `nim_model_metadata.py`.

## Attribution

- NIM **silently accepts** `HTTP-Referer` and `X-Title` headers (probe — sent
  with both, response was a normal 200; no behavior change observed).
- **No equivalent to OpenRouter's App Activity** dashboard. NIM provides
  no per-app usage breakdown.
- Per-request correlation: response includes `nvcf-reqid: <uuid>` header.
  FreeRide should log this on errors for support cases (probe — observed on
  every successful response).
- **Implementation:** `attribution_headers()` returns `{}` for NIM. The
  OpenRouter pattern doesn't apply.

## OpenAI-compat deltas

**Request side (NIM accepts these in addition to OpenAI's set; probe-verified):**
- `min_tokens` (int) — minimum tokens to generate
- `top_k` (int) — top-k sampling
- `repetition_penalty` (float)
- `frequency_penalty` (float — also OpenAI-standard but verified accepted)
- `nvext` (object) — NIM-specific overrides; opaque pass-through if needed

**Response side (extras; varies by model):**

*Classic NIM shape* (e.g. `meta/llama-3.1-8b-instruct`):
- adds `nvext.worker_id`, `nvext.timing` (per-stage ms), `nvext.kv_hit_rate`,
  `nvext.router_queue_depth`
- `message.reasoning_content` (null for non-reasoning models)
- `usage.prompt_tokens_details.cached_tokens` and `audio_tokens`

*vLLM-extended shape* (e.g. `meta/llama-3.1-70b-instruct`):
- adds top-level `service_tier`, `system_fingerprint`, `prompt_logprobs`,
  `prompt_token_ids`, `kv_transfer_params`
- `message`: adds `refusal`, `annotations`, `audio`, `function_call`,
  `tool_calls`, `reasoning`, `reasoning_content`, `token_ids`
- `choices[].stop_reason` (token id, in addition to `finish_reason`)

**Strip before forwarding to FreeRide clients:** `nvext`, `stop_reason`,
`token_ids`, `prompt_token_ids`, `prompt_logprobs`, `kv_transfer_params`,
`service_tier` (when null), `reasoning_content` (when null). Keeping these
risks clients erroring on unexpected fields.

**No-op headers from request side:** OpenRouter's
`HTTP-Referer`/`X-Title` are accepted but ignored; safe to send.

## Context overflow behavior

A 40,035-token prompt sent to llama-3.1-8b-instruct (128K context) was
processed without error — `prompt_tokens: 40035` came back in usage (probe).
NIM does **not** reject oversized prompts at the gateway; it's the model's
context window that limits. There's no clean "context overflow" error to
classify. FreeRide should rely on per-model context-length metadata to refuse
client-side, or accept the model's native error if it appears.

## Open questions

These were not resolvable from public docs or live probing:

- Exact `429` body shape and whether NIM emits `Retry-After`. Need to be
  caught and mapped opportunistically once observed in the wild.
- Exact `402` (quota-exhausted) response shape — unverified.
- Per-key rate limit numerics (req/min, tokens/min) — not documented.
- Per-model context lengths and capability flags — must be maintained
  out-of-band by FreeRide (see "Catalog" above).
- Whether `nvext` request-side fields map to documented vLLM levers.
- Whether NIM ever returns `finish_reason: "content_filter"` (NIM has guard
  models like llama-guard-4-12b but their integration with chat-completions
  isn't documented).

## Sources

- API base & auth: live probe, https://integrate.api.nvidia.com/v1
- Catalog field shape, duplicates, no-filter behavior: live probe of
  `GET /v1/models` (2026-05-07)
- Bad-auth shape (403): live probe with bogus token
- Bad-model shape (404 plaintext): live probe with `unknownco/some-model`
- Streaming format / `[DONE]` / usage in penultimate event: live probe with
  `stream:true`
- Tool call & JSON mode response shape (vLLM-extended): live probes against
  `meta/llama-3.1-70b-instruct`
- Vision request/response: live probe with base64-PNG against
  `meta/llama-3.2-11b-vision-instruct`
- Context overflow: live probe with 40K-token prompt
- High-level NIM overview: https://docs.nvidia.com/nim/index.html
- Catalog UI (model cards / build.nvidia.com): https://build.nvidia.com/models
- Reference index (sparse): https://docs.api.nvidia.com/nim/reference/llm-apis
- OpenAPI spec: not exposed at any standard path (probe).
