# Future-provider Protocol-fit survey

> Phase 0 design validation: confirming the Provider Protocol from
> the design plan absorbs Groq, Cloudflare Workers AI, and HuggingFace
> Inference Providers without redesign. Not for immediate implementation —
> Phases 1–3 ship with OpenRouter and NVIDIA NIM only. This is the
> seam-quality stress test.

## Groq

1. **Base URL + auth.** `POST https://api.groq.com/openai/v1/chat/completions`; `Authorization: Bearer $GROQ_API_KEY`. (https://console.groq.com/docs/api-reference)
2. **Free-tier semantics.** Explicit Free vs Developer plans. Free has per-model RPM/TPM/RPD/TPD caps (e.g. `llama-3.1-8b-instant`: 30 RPM, 6K TPM, 14.4K RPD, 500K TPD). No `:free` suffix or metadata flag — *which* models are accessible is plan-dependent and the catalog doesn't expose that. **Free-detection must be a hardcoded allowlist inside the provider plugin**, refreshed against the published rate-limits page. (https://console.groq.com/docs/rate-limits, https://groq.com/docs/service-tiers)
3. **OpenAI-compatibility.** Substantial. Same `/openai/v1/` path, same request/response shape. Deltas: `x_groq` extension field on responses; `logprobs` and `frequency_penalty` documented as "not yet supported". (https://console.groq.com/docs/api-reference)
4. **Catalog.** `GET /openai/v1/models` returns `id`, `owned_by`, `context_window`. **No free-eligibility flag.** (https://console.groq.com/docs/api-reference)
5. **Streaming.** SSE, `data: [DONE]` terminator — OpenAI-shape. (https://console.groq.com/docs/api-reference)
6. **Rate-limit signal.** HTTP 429. Headers: `x-ratelimit-limit-requests`, `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-*`, `x-ratelimit-reset-*`, and `Retry-After` (seconds, only when 429 returned). JSON error body shape not documented. (https://console.groq.com/docs/rate-limits)
7. **Quirks forcing Protocol changes?** None. Hardcoded free-list lives inside the plugin.

## Cloudflare Workers AI

1. **Base URL + auth.** Two surfaces. **Native:** `https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{model}` — non-OpenAI-shaped (`{prompt: "..."}` in, `{result, success, errors}` out). **OpenAI-compatible:** `https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions`. Both: `Authorization: Bearer {api_token}`. **Account ID is part of the URL, not the key** — the plugin needs both. (https://developers.cloudflare.com/workers-ai/get-started/rest-api/, https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)
2. **Free-tier semantics.** Universal: **10,000 Neurons/day at no charge** across all plans, resetting at 00:00 UTC. Beyond: $0.011 / 1,000 Neurons. Neuron cost is per-(model, token-direction) — e.g. IBM Granite Micro = 1,542 neurons/M input tokens; DeepSeek R1 = 443,756 neurons/M output tokens. **"Free" is a global daily budget, not a per-model designation.** No documented programmatic way to query remaining Neurons or detect "this request was free vs billed." (https://developers.cloudflare.com/workers-ai/platform/pricing/)
3. **OpenAI-compatibility.** Drop-in for chat completions on the `/ai/v1/` path. Schema deltas not documented (assume parity until proven otherwise). (https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)
4. **Catalog.** Documentation page at `/workers-ai/models/`; no programmatic catalog endpoint shown. The `/ai/v1/models` OpenAI-compat endpoint may exist but isn't documented; needs verification at implementation time.
5. **Streaming.** Not documented in REST/pricing pages. Confirm at implementation.
6. **Rate-limit signal.** Not documented in the pages reviewed. Standard 429 likely.
7. **Quirks forcing Protocol changes?** None at the Protocol level. The account-ID-in-URL is a Provider construction concern (`__init__(account_id=...)`), not a Protocol method signature. The "free is a daily budget, not a per-model flag" pattern is also handled inside the plugin: `list_free_models()` returns "all eligible OpenAI-compat models; resolver decides whether the day's Neuron budget can afford one."

## HuggingFace Inference Providers

1. **Base URL + auth.** `https://router.huggingface.co/v1`; `Authorization: Bearer $HF_TOKEN`. (https://huggingface.co/docs/inference-providers/index)
2. **Free-tier semantics.** Monthly credit budget, **not** per-model: Free $0.10/mo, PRO $2/mo, Team/Enterprise $2/seat/mo. After exhaustion: pay-as-you-go (requires credit purchase). Same model can cost different amounts depending on which upstream provider HF routes to. (https://huggingface.co/docs/inference-providers/pricing)
3. **OpenAI-compatibility.** Explicitly drop-in for chat completions. Suffixes on model id select routing policy: `:fastest`, `:cheapest`, `:preferred`, or `:<provider>` (e.g. `deepseek-ai/DeepSeek-R1:sambanova`). Not available for non-chat tasks (image/embedding) on this endpoint. (https://huggingface.co/docs/inference-providers/index)
4. **Catalog.** `GET /v1/models` returns the cross-provider model list. (https://huggingface.co/docs/inference-providers/index)
5. **Streaming.** SSE matching OpenAI. (Implied by drop-in compatibility; explicit example shows `stream: false` but the SDK supports streaming.)
6. **Rate-limit signal.** Not explicitly documented. Quota-exhausted likely surfaces as a 4xx requiring credit purchase rather than a transient 429.
7. **Quirks forcing Protocol changes?** None. The most interesting wrinkle — model IDs carry routing policy suffixes — fits cleanly into `Model.api_id`. Org billing via `X-HF-Bill-To` could be exposed via `attribution_headers()` if a user opts in, but it's optional and out of scope for v3.0.

## Protocol absorption verdict

**Yes — the Protocol from the design plan absorbs all three providers without any changes.** No D1–D14 decision needs revising.

Two heterogeneity patterns surface that are all handled *inside* the plugin, not at the Protocol level:

- **Per-model free flag (OpenRouter pattern)** vs **global free budget (CF, HF, NIM credits pattern)** vs **per-model RPM/TPM caps (Groq pattern).** Each plugin's `list_free_models(key)` is the single function that hides this — for CF/HF it returns models tagged with cost hints; for OpenRouter it returns models with `:free` suffix; for Groq it returns the hardcoded allowlist; for NIM it returns models with positive credit balance.
- **Construction config that isn't a key** (CF account_id; potentially NIM region or HF bill-to). Handled in `Provider.__init__`, not in the Protocol's runtime methods. Plugin discovery via Python entrypoints (the design plan) already supports per-plugin configuration.

## Recommended Protocol changes (if any)

None required for v3.0. Three optional additions worth considering only if Phase 3 NIM integration surfaces friction:

- **`def quota_state(key) -> Optional[QuotaSnapshot]`** — would let the resolver predict exhaustion (CF Neurons remaining, HF credits remaining, NIM credits) instead of discovering it reactively via `ErrorKind.QUOTA_EXHAUSTED`. D13 currently says "compute from local request history," which works only when our gateway sees every request through that key. Adding this method later is a non-breaking bump from `api_version=1` to `api_version=2`.
- **`Model.cost_hint: Optional[float]`** — useful for stretching free budgets on CF/HF but unnecessary for OpenRouter/Groq/NIM where models are either free or not.
- **`async def forward_request(request, route)`** instead of separate chat/embeddings methods — only relevant when D2 (chat-only) is revisited at Phase 6+.

Ship Protocol as v1; revisit only on real friction.

## Sources

- https://console.groq.com/docs/api-reference
- https://console.groq.com/docs/rate-limits
- https://groq.com/docs/service-tiers
- https://developers.cloudflare.com/workers-ai/get-started/rest-api/
- https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://huggingface.co/docs/inference-providers/index
- https://huggingface.co/docs/inference-providers/pricing
