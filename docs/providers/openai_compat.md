# Spec-driven OpenAI-compatible providers

> Reference for `freeride/providers/openai_compat.py` and the table in
> `freeride/core/provider_specs.py`. Most free-tier gateways differ only in data; this is
> where that data lives.

## Adding a provider

Add one `OpenAICompatSpec` row to `PROVIDER_SPECS`. The minimum is a name, display name,
base URL, env var, signup URL and a one-line description:

```python
OpenAICompatSpec(
    name="acme",
    display_name="Acme AI",
    base_url="https://api.acme.ai/v1",
    env_vars=("ACME_API_KEY",),
    signup_url="https://acme.ai/keys",
    description="Acme — 1,000 requests/day on :free models",
    free_id_suffix=":free",
    daily_requests=1_000,
)
```

That row is automatically:

- wired to `ACME_API_KEY` (plus `ACME_API_KEY_2`, `_3`, …) in `provider_env`;
- loaded by `freeride serve` when the env var is set;
- offered by `freeride init` when `default_on=True`;
- shown by `freeride keys` and `freeride doctor`;
- run through the conformance suite (`tests/conformance`, parametrised over every row).

Add a short behaviour test in `tests/providers/` only for quirks you set on the spec.

## Free-model detection

First match wins:

| Order | Rule | Spec field |
|---|---|---|
| 1 | `ACME_FREE_MODELS_OVERRIDE=a,b` env var (user pin) | always on |
| 2 | explicit allowlist | `free_ids` |
| 3 | predicate over the raw catalog entry | `free_predicate` |
| 4 | id suffix (`:free`) | `free_id_suffix` |
| 5 | `pricing.prompt == 0` / `pricing.input == 0` in the entry | automatic |
| 6 | everything the catalog lists | fallback |

When `GET /models` fails and the spec has `fallback_models`, those ids are surfaced instead
(some keyless gateways gate the catalog behind auth while chat is open).

## Tool capability

The agent ladder only admits models whose `supported_parameters` contains `"tools"`. The
adapter reads whatever self-reported flag the catalog carries (`supported_parameters`,
`capabilities.tools`, `supports_tool_calling`, `function_call`), then `metadata[id]`, then
`tools_default`. Those are claims; `freeride audit-models` is the authority and demotes
models that fail live.

## Quota shape

- `cooldown_scope="key"` (default): a 429 benches the key for the provider, as for every
  other plugin.
- `cooldown_scope="model"`: a 429 benches only the (provider, model) pair in the
  model-health cache for `RECENT_FAILURE_TTL_SEC` and leaves the key routable. Use it when
  the provider meters each model separately (SambaNova: 20 requests/day per model;
  OVHcloud anonymous: 2 req/min per model).
- `rpm`, `daily_requests`, `daily_tokens` are the published caps, recorded for display and
  for the pacing work in the roadmap; the adapter does not yet pre-empt them.

## Retry hints

`retry_after_hint()` reads, in order: the `Retry-After` header (delta-seconds or HTTP
date), `x-ratelimit-reset-*` headers, then the error body: structured fields
(`retryDelay: "17s"` as Gemini sends inside `error.details[]`, `retry_after: 60`,
`retryAfterSeconds`), then anchored prose ("try again in 30 seconds", "retry after 2m").
Everything is clamped to 24 hours and only the number is kept.

## Error classification

| Status | Kind | Notes |
|---|---|---|
| 401 | AUTH | |
| 402 | QUOTA_EXHAUSTED | credit wallets |
| 403 | MODEL_NOT_FOUND if the body says premium / subscription / upgrade; QUOTA_EXHAUSTED if it says balance / quota; else AUTH | tier gates are about the model, not the key |
| 404 | MODEL_NOT_FOUND | unknown ids are usually a bare 404 page |
| 400 / 410 / 422 | MODEL_NOT_FOUND or QUOTA_EXHAUSTED on body patterns, else UNKNOWN | `model_retired`, "insufficient balance" |
| 408 / 504 | TIMEOUT | |
| 429 | RATE_LIMIT | with retry hint |
| 5xx | UNAVAILABLE | |

## Request shaping

- `drop_params`: top-level fields the upstream rejects (Ollama Cloud: `tool_choice`,
  `logprobs`, `top_logprobs`, `n`).
- `force_single_tool_call`: sets `parallel_tool_calls=false` when tools are present.
- `extra_headers`: sent on every request. Routeway, Septor and CLōD sit behind a Cloudflare
  rule that rejects non-browser User-Agents, so their rows carry one.
- `timeout_s` / `read_timeout_s`: connect/write budget and the non-streaming read budget.
  Hidden-reasoning models (GLM Flash, Agnes) need 90-180 s before the first byte; streaming
  always gets a 600 s read budget.

## Model substitution

Some gateways answer a named free route with a different model under HTTP 200. The adapter
compares the response's `model` with the requested id (normalised: provider prefix,
`:free` suffix, case and separators dropped), logs a warning and increments
`substitutions_seen`. Routes named `auto`, `*/free`, `*/auto` or `free_smart` are exempt
because they legitimately report the upstream id.

With `strict_model_match=True` the mismatch is raised as a synthetic 404 that
`classify_error` reads as `MODEL_NOT_FOUND`, so the failover chain skips the id instead of
handing an agent a model it did not ask for. Enabled for BlockRun (live on 2026-10-16: two
of three free ids answered as `nvidia/llama-3.2-11b-vision`), Septor Labs and Lucidity.

## Keyless providers

`keyless=True` rows have no env var. They are configured by naming them in
`FREERIDE_KEYLESS` (comma list, or `all`), never by default:

```
FREERIDE_KEYLESS=ovh,blockrun
```

The failover chain carries the pseudo-key `anonymous` for them so cooldown and health
bookkeeping work unchanged; `auth_header()` returns `{}`. Each keyless row states its
`data_use`; `freeride init` prints it. Kilo's `:free` routes may be used for training, so
its row says so and is not suggested.

## Gemini

`gemini` uses `GeminiCompatProvider`, a thin subclass:

- Gemini 3 attaches a `thought_signature` to each function call it emits
  (`tool_calls[i].extra_content.google.thought_signature`) and rejects a later request that
  replays that call without it. OpenAI-format clients and FreeRide's own translators drop it.
  The subclass caches signatures by tool-call id and by a name+arguments fingerprint (agents
  rewrite ids), restores them on the way out, and sends Google's documented
  `skip_thought_signature_validator` sentinel when nothing is cached.
- Tool definitions are scrubbed of `$schema`, `strict`, `additionalProperties`, `$id` and
  `$comment`, which Gemini's schema validator rejects.
- Catalog ids are listed as `models/gemini-2.5-flash`; the `models/` prefix is stripped.
- The free predicate keeps Flash / Flash-Lite / Gemma rows and drops Pro, embedding, image,
  TTS and live models.

## What is not in the adapter yet

- Pre-emptive pacing against `rpm` / `daily_*` (the router still learns from 429s).
- Key validation endpoints other than `GET /models` (`validate_path` is reserved).
- `freeride/<preset>` ids on `/v1/chat/completions` (they are resolved on the Anthropic and
  fx routes only; the OpenAI route forwards them literally, as before).

## Verified live (2026-10-16, Daytona sandbox + local, zero keys)

| Provider | Catalog | Probe | Tool call via gateway | Notes |
|---|---|---|---|---|
| OVHcloud anonymous | 10 chat models after the chat-shape filter | ok | yes (`gpt-oss-20b`) | 429 on the third request within a minute, as documented; `parallel_tool_calls=false` is rejected with 400 |
| Kilo anonymous | 11-12 `:free` models | ok | yes (three models) | `nemotron-3.5-super-vl` spends small `max_tokens` on reasoning and returns empty content |
| BlockRun | 7 `billing_mode: free` models | ok | yes (`llama-3.2-11b-vision`) | two other ids substituted under HTTP 200; now a failover signal |
| Ollama Cloud (Free key) | 7 allowlisted of 21 listed | ok | yes (`nemotron-3-super`, `gemma4:31b`, `beam`) | gated ids answer 402 "not in the Free plan" → MODEL_NOT_FOUND; reasoning streams in `delta.reasoning` |
| xKiro (free key) | 51 `access_tier: free` | ok | yes (`ministral-14b`, Qwen `:free`) | streaming ok |
| Gemini (key) | 14 after excluding 2.x | 402 (one intermittent 200 on `gemma-4-31b-it`) | no | project in prepaid-billing mode: "prepayment credits are depleted"; benched as QUOTA_EXHAUSTED |
| SambaNova (key) | 5 | 402 | no | `PAYMENT_METHOD_REQUIRED` on every model |
