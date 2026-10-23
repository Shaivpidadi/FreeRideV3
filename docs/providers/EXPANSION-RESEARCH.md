# Free-tier provider landscape and expansion plan

> Research record for widening FreeRide's provider set beyond the seven it ships today.
> Every free-tier claim below was checked against the provider's **own** pricing, rate-limit,
> or terms page on **2026-10-16**. Nothing here comes from a live key probe; where a provider
> publishes no number, the cell says so instead of guessing. Re-verify before shipping any row:
> free tiers churned materially in the last six months (three of FreeRide's current README
> claims are already stale, see §2).

---

## 1. Summary

FreeRide ships 7 providers. The wider market has roughly **55 gateways and first-party APIs**
that advertise a no-card free tier with an OpenAI-compatible chat endpoint. After filtering for
what a coding-agent gateway actually needs (recurring quota, no card, tool calling, streaming,
reachable from outside mainland China, a defensible ToS), the picture is:

| Bucket | Count | What to do |
|---|---|---|
| **A. First-party, high value, ship in-tree** | 5 | Google Gemini, Mistral, SambaNova, Ollama Cloud, Z.ai (GLM Flash). Each adds a real frontier-adjacent model family and a quota pool independent of OpenRouter. |
| **B. Keyless, zero-signup fallbacks (opt-in)** | 3 | OVHcloud anonymous, Kilo Gateway, BlockRun. Work with no account at all; useful as the very last rung of the ladder. Off by default because some log or train on prompts. |
| **C. No-card aggregators worth a generic adapter** | ~17 | xKiro, Requesty, Routeway, Septor, ElectronHub, Lucidity, DreamPrompting, Waterfall, Api.Airforce, BazaarLink, OrcaRouter, UnoRouter, LLM7, AnyAPI, Typhoon, Speka, LongCat. Individually small; collectively a lot of daily requests. Only worth it once a **declarative OpenAI-compatible plugin** exists (§5) so each costs ~10 lines, not ~260. |
| **D. Skip** | ~22 | Card required, no $0 models, retired, no tool calling on the free tier, PRC real-name wall, unreachable, or ToS forbids personal use. Listed with reasons in §4.4. |

The single highest-leverage engineering change is not any one provider: it is a
**generic OpenAI-compatible provider class driven by a spec table**, with keyless mode,
account-wide quota pools, and a metadata sidecar for tool capability. With that in place,
bucket A costs about a week of focused work, bucket C about two days, and future providers
are a data change plus a conformance test.

---

## 2. Corrections to FreeRide's current claims

These were found incidentally and should be fixed in `README.md`, `freeride/cli/cmd_init.py`
and `docs/providers/SURVEY.md` regardless of whether anything else in this document ships.

| Provider | What FreeRide says today | What the provider's page says on 2026-10-16 |
|---|---|---|
| **Cerebras** | "Free tier with RPM/TPM limits", "RPM / TPM caps" | **No renewing free tier.** A one-time **$5** promotional credit, granted only after a **verified payment method** is added, expiring **30 days** after grant. Trial limits: 5 RPM, 30K uncached TPM, 1M TPD on `gpt-oss-120b` and `qwen-3.8-27b`. Docs state Cerebras "doesn't currently offer a no-cost tier that renews automatically". FreeRide should demote Cerebras to "trial only, card required" or drop it from `freeride init`. |
| **HuggingFace** | "$0.10/mo Free, $2/mo PRO" | **Free accounts receive no monthly credit** ("Free Users — Monthly Credits: None — extra usage: credits purchase required"). PRO is $2.00/month. The free row in the README is wrong; the plugin only works for PRO subscribers or users who bought credits. |
| **NVIDIA NIM** | "credits per account" | Access is a recurring **per-account rate limit**, not depleting credits. NVIDIA publishes no number on a public page; the widely quoted 40 RPM comes from the developer forum only. Trial terms scope usage to evaluation/prototyping. |
| **OpenRouter** | "rotating free models" | Still true, but worth stating the cap: `:free` routes are **20 RPM and 50 requests/day account-wide**, rising to **1,000/day** once the account has ever bought 10 credits. `GET /api/v1/key` reports `free_model_daily_requests`, which the gateway could read to pre-empt the daily 429. |

---

## 3. Method and scope

- **Sources.** Official pricing, rate-limit, API-reference and terms pages, plus each gateway's public `GET /v1/models` where it answers unauthenticated. Third-party roundups were used only to locate official pages, and are labelled where an official page was unreachable (several Chinese platforms block non-PRC resolvers).
- **Scope.** Chat completions only, because that is FreeRide's surface. Web-search APIs (Tavily, Exa, Firecrawl, Linkup, Parallel), text-to-speech, image and video endpoints were excluded. Embeddings are noted where a candidate has them.
- **Not done.** No keys were created and no requests were sent. "Tools: yes" means the provider documents `tools`/`tool_calls` on the chat endpoint or flags it per model in its catalog; it does not mean the model emits correct tool calls under Claude Code's 70-tool request bodies. That needs the live probe FreeRide already has (`freeride audit-models`) extended with a tool-call smoke test (§5.6).

### 3.1 Inclusion criteria (in priority order)

1. **Recurring** free allowance (daily, weekly or monthly reset), not a one-time grant.
2. **No payment method** required to obtain or keep the key.
3. **OpenAI-compatible** `POST /v1/chat/completions` with SSE streaming.
4. **Tool calling** available on at least one free model.
5. **Usable from outside mainland China** without PRC identity verification.
6. **Published limits**, so the router can budget instead of discovering 429s.
7. **Quota-safe key validation**: some probe that proves auth without spending metered quota.
8. **ToS** that does not forbid personal use or a self-hosted single-user proxy.
9. **Data use** disclosed (training on prompts is a disqualifier for default-on, acceptable for opt-in).
10. **Operator maturity**: a company or well-run community project, not a weekend domain. Several bucket-C gateways are solo-maintained; that is fine for an opt-in rung, not for a default.

---

## 4. Landscape

Numbers are quoted from the provider's page; "n/p" = not published. "Base URL" is the
OpenAI-compatible endpoint. Tools/stream: Y = documented, L = likely (catalog flag or SDK
wiring but not spelled out), N = documented as unsupported, ? = unknown.

### 4.1 Bucket A: first-party APIs to ship in-tree

| Provider | Free allowance | Card | Limits (official) | Free models worth routing | Base URL | Tools / stream | ToS & data | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Google Gemini API** | Recurring, free tier per model ("Free of charge" rows on pricing) | No | Per-model RPM/TPM/RPD **removed from public docs**; visible only after login in AI Studio. Spend limit "N/A". | `gemini-2.5-flash`, `gemini-2.5-flash-lite`, 3.x Flash/Flash-Lite family, `gemma-4` | `https://generativelanguage.googleapis.com/v1beta/openai/` | Y / Y (compat endpoint lists streaming and function calling, marked beta) | Terms (eff. 2026-03-23): "for professional or business purposes, not for consumer use"; unpaid tier content may be used to improve products and read by humans; EEA/UK/CH users "may use only Paid Services"; 18+. | **Ship.** Biggest single quality gain available. Use the OpenAI-compat endpoint first (no new wire format). Two known quirks: Gemini 3 requires the `thoughtSignature` from a tool call to be echoed in later turns (cache it keyed by tool-call id, or send Google's documented sentinel), and its schema validator rejects some JSON-Schema keywords clients send by default. Region-gate the plugin: hide it in `freeride init` for EEA/UK/CH locales. |
| **Mistral (La Plateforme)** | Recurring "Free mode" with "included monthly usage within the limits shown on the Limits page" | Not stated | **n/p** on public pages (console only). The older public figures (1 RPS, 500K TPM, 1B tokens/month) have been removed. | Not enumerated publicly; expect Mistral Small / Devstral / Codestral class | `https://api.mistral.ai/v1` | Y / Y | Pricing shows "model training: opt-out" per plan; phone verification historically required for Free mode, now undocumented. | **Ship.** First-party, mature, tools work well, Codestral/Devstral are strong coding models. Treat limits as unknown: start with conservative per-key pacing and learn from 429 `Retry-After`. |
| **SambaNova Cloud** | Recurring **Free tier** ("applied when there is no payment method linked") | No | **20 RPM, 20 RPD, 200,000 TPD** per model on `DeepSeek-V3.1`, `Meta-Llama-3.3-70B-Instruct`, `gpt-oss-120b`, `DeepSeek-V3.2`, `gemma-4-31B-it`. Developer tier (card) = 20M tokens/day. | the five above | `https://api.sambanova.ai/v1` | Y / Y (`n>1` with tools returns 400) | None restrictive found. | **Ship.** Note this reverses the mid-2026 consensus that SambaNova's free tier was gone; the rate-limits page now documents a no-card tier. 20 requests/day is tiny per model but DeepSeek-V3.x at full speed is a strong top-of-ladder candidate. Must model the **per-model daily** cap or it will burn through in one agent turn. |
| **Ollama Cloud** | Recurring monthly "starter amount of usage for a smaller set of starter models"; resets monthly from signup date | Not stated | **1 concurrent request** on Free; no token figure published; "starter" model list not published | Cloud catalog carries `gpt-oss`, `gemma4`, `glm-5.3-flash`, `deepseek-v4.1-flash`, `kimi-k2.7-code`, `nemotron-3-nano`, Qwen; which are "starter" needs a probe | `https://ollama.com/v1` | Y / Y (`tool_choice`, `logprobs`, `n` unsupported; reasoning arrives in `message.reasoning`) | Nothing restrictive found. | **Ship, after a probe.** Many catalog rows answer 403 "requires a subscription" on Free; plugin needs a per-model allowlist discovered by probing, plus concurrency = 1 enforced locally. |
| **Z.ai (Zhipu international)** | Recurring: `GLM-4.7-Flash`, `GLM-4.5-Flash`, `GLM-4.6V-Flash` listed "Free" on pricing | No | RPM/concurrency behind login (community reports 1 concurrent on Flash) | the three Flash models (`glm-5.3-flash` is paid) | `https://api.z.ai/api/paas/v4/` | Y / L (separate "stream tool call" guide) | Z.ai (Singapore) terms contain an anti-traffic-redirect clause with no personal-use carve-out; the mainland console (`open.bigmodel.cn`) has a research carve-out but was unreachable from a non-PRC resolver. | **Ship.** GLM-4.7-Flash is a hidden-reasoning model: TTFB of 30-60 s is normal, so the plugin needs a 90 s first-byte budget or the ladder will mark it dead. Keys from the two consoles are not interchangeable; validate against `api.z.ai` first and fall back to `open.bigmodel.cn`. |

**Post-implementation live check (2026-10-16, real keys).** Ollama Cloud's Free plan is
exactly `gpt-oss:20b`, `gpt-oss:120b`, `nemotron-3-nano:30b`, `nemotron-3-super`,
`nemotron-3-ultra`, `beam`, `gemma4:31b` (every other catalog row answers 402 "not in the
Free plan"); tool calls and streaming work. xKiro works as documented (51 free-tier ids,
tool calls, streaming). **SambaNova** contradicted its rate-limits page: a fresh account
answers 402 `PAYMENT_METHOD_REQUIRED` on every model, so it is shipped opt-in and should be
read as card-required. **Gemini** retired every `gemini-2.x` id for new users (404) and a
project created with prepaid billing answers 402 instead of serving the free tier; keys must
come from a project without billing. **Mistral**'s console did not offer Free mode to the
account tested, so it is opt-in too.

Two first-party names that did **not** make bucket A:

- **Cohere**: trial keys are generous (20 RPM, 1,000 calls/month, compat endpoint with tools) but the Terms of Use say the service is "designed for business use" and must not be used "for personal, family or household purposes", and §4.1 allows Cohere to use API data to improve its products. FreeRide's users are individuals. Skip.
- **OpenCode (formerly "Zen", now "Console")**: free models (`glm-5.3-flash-free`, `deepseek-v4.1-flash-free`, `nemotron-3.5-lightning-free`, `mimo-v2.6-flash-free`, `big-pickle`) are "free for a limited time", setup asks for billing details, and through mid-2026 the free routes answered 403 to any client other than the OpenCode CLI. The current docs say the key works "with OpenCode or another coding agent" and the base URL moved to `https://opencode.ai/inference/openai/v1`. **Conditional**: worth a probe, not worth code until a probe shows a 200 from a plain client.

### 4.2 Bucket B: keyless fallbacks (opt-in only)

These need no account. They are the right last rung for a user who has not configured any
key yet, and the wrong default for everyone else: anonymous tiers are shared per IP, slow,
and in two cases explicitly train on prompts.

| Provider | Allowance | Limits | Models | Base URL | Tools / stream | Data use | Notes |
|---|---|---|---|---|---|---|---|
| **OVHcloud AI Endpoints (anonymous)** | Recurring, no key | **2 req/min per IP per model**; keyed tier (400 rpm) needs a Public Cloud project with a payment method | `gpt-oss-120b`, `gpt-oss-20b`, `Meta-Llama-3_3-70B-Instruct`, `Qwen3.8-27B`, `Qwen3.5-9B` | `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1` | Y / Y (streaming tool calls documented; Llama models: single tool call only) | No training clause found | Real company, well-documented. The per-model 2 rpm means the plugin should spread calls across models rather than retry the same one. |
| **Kilo AI Gateway** | Recurring, anonymous `:free` routes | **200 req/hour per IP**, HTTP 429 beyond | `nvidia/nemotron-3-super-120b-a12b:free`, `google/gemma-4-26b-a4b-it:free`, `cohere/north-mini-code:free`, others | `https://ai-gateway.kilo.ai/api/v1` | Y / Y (`supported_parameters` lists `tools`, `tool_choice`) | **Every `:free` model is flagged `mayTrainOnYourPrompts: true`**; NVIDIA rows say "trial use only, do not submit personal or confidential data" | Strongest keyless roster, worst privacy posture. Opt-in with an explicit warning. |
| **BlockRun** | Ongoing, no account | **30 req/min or 300 req/hour per IP** (`FREE_TIER_RATE_LIMITED`); 120 s upstream timeout | Nemotron 3 Ultra 550B, Nemotron 3.5 Lightning, Cohere North Mini Code, Llama 3.2 11B Vision, Muse Glimmer 30B | `https://blockrun.ai/api/v1` (per docs; `api.blockrun.ai` answers 401) | Y / Y | "We never sell user data"; ToS not read | Paid tier is USDC via x402, which says something about the operator's audience. Fine as a keyless rung. |
| **AI Horde** | Community volunteer GPUs, anonymous key `0000000000` | Lowest queue priority; ETA 0-260 s observed; default `max_tokens` 128 | Rotating community GGUFs (`google/gemma-4-31b` had workers at check time) | `https://oai.aihorde.net/v1` | **N** / Y | Non-profit; "pilot, may be restricted" | **No tool calling**, so it can never serve agent traffic. Only useful for `freeride/free` plain-chat requests, and only as a demo of "zero keys still answers". Low priority. |

### 4.3 Bucket C: no-card aggregators (generic adapter candidates)

All are OpenAI-compatible bearer-token gateways. Ranked roughly by (allowance × model quality × operator maturity). "Spread" means how the allowance is shared.

| # | Provider | Allowance (official) | Spread | Tools / stream | Notable free models | Base URL | Friction & caveats |
|---|---|---|---|---|---|---|---|
| 1 | **xKiro** | **500,000 tokens/day** on free-tier models (1M/day + 30M bonus for 30 days after Telegram verification) | account-wide | Y / Y (51 models flagged `access_tier: free`, nearly all `tools: true`) | `qwen/qwen3-coder-plus:free`, `qwen/qwen3.8-max:free`, `mistralai/devstral-medium` | `https://api.xkiro.com/v1` (also `/v1/messages`) | Public `/v1/models` answers 200 without a key, so validation must hit `/v1/usage`. Non-streaming requests cut at 95 s. |
| 2 | **Requesty** | **200 requests/day** on free models | account-wide | Y / Y | `google/gemma-4-31b-it`, `nvidia/nemotron-3-super-120b-a12b`, `mistral/leanstral-1-5` (13 zero-price) | `https://router.requesty.ai/v1` (EU: `router.eu.requesty.ai/v1`) | Router does not train, but most NVIDIA free rows are flagged `data_used_for_training: true` upstream. Catalog exposes `supports_tool_calling`. |
| 3 | **Routeway** | **200 requests/day, 1 concurrent**, low-priority shared queue | account-wide | Y / Y (all 9 `:free` list `tools`) | `deepseek-v4-flash:free`, `minimax-m2.7:free`, `muse-glimmer-30b:free` | `https://api.routeway.ai/v1` | Cloudflare in front rejects non-browser User-Agents (error 1010): send a browser-style UA. "We never log prompts." |
| 4 | **Septor Labs** | **50 req/day and 500K tokens/day** on free models; Free plan 20 RPM / 100K TPM; $1 signup credit (90 d) | account-wide | per-model "Tools" tag / Y | "Qwen3 Coder free", "glm 4.5 air free", "Llama 3.3 70B free" | `https://api.septorlabs.com/v1` | Same Cloudflare UA challenge as Routeway. Some free ids were observed answering as a different model: verify `response.model == request.model` and fail over on mismatch. |
| 5 | **ElectronHub** | **$0.25/week** credits (Sunday 21:00 UTC, no rollover) + **10 "Neutrinos"/day** for `:free` models; **5 RPM**, 3-min cooldown after 429 | account-wide | Y / Y (22 `:free`, `function_call: true`) | `gpt-oss-120b:free`, `deepseek-v4-flash:free`, `qwen3-coder-30-a3b-instruct:free` | `https://api.electronhub.ai/v1` | Public model list; validate on `GET /v1/user/me`. Has been seen returning an HTTP-200 body containing a proxy-error banner: treat a leading "error" heading as a failure. |
| 6 | **Lucidity Composite** | **80 requests/day** shared across `:free` models | account-wide | ? (tools documented on `/v1/responses` only) / Y | `deepseek/deepseek-v4.1-flash:free`, `z-ai/glm-5.3-flash:free`, `deepseek/deepseek-v4-flash-0731:free` | `https://composite.lucidity.sh/v1` | Response omits the `:free` suffix; `open/*` routes were seen silently answering as the house model. |
| 7 | **DreamPrompting** | **5,000 req/day, 500K tokens/day** per account (rolling 24 h), 100 rpm per IP; 32K input / 8K output caps | account-wide | ? (open-source shim has a `supports_tools` flag) / Y | `auto` fail-over route; `groq/openai/gpt-oss-120b`, Llama 3.3 70B via upstream free tiers | `https://dreamprompting.com/api/v1` | It is itself a proxy over 27 upstream free tiers, so their terms apply transitively. Privacy policy silent on API logging. |
| 8 | **Waterfall** | "$0/month, community rate limits" (numbers n/p) | account-wide | Y / Y (`free_smart` route "prioritizes free, tool-capable models") | `nemotron-3-ultra-550b-a55b`, `gemma-4-31b-it`, `glm-5.2` ("131+ free models") | `https://api.getwaterfall.org/v1` | Solo-maintained. Prompts pass through to upstream; ZDR only on `privacy_smart` routes. 503 with structured `detail.code = upstream_unavailable` on outages. |
| 9 | **Api.Airforce** | **1 request/min, 1,000/day**, free-tier models only | account-wide | Y / Y (per-model flags) | `gpt-oss-20b`, `glm-4.7-flash`, `kimi-k2.7-code`, `llama-instant` | `https://api.airforce/v1` (also `/v1/messages`) | 1 rpm makes it a last-rung fallback only. No ToS page found. |
| 10 | **BazaarLink** | `auto:free`: **10 req/min, 60 req/day** (×2 after any top-up); free accounts 20 RPM | account-wide | Y / Y (streamed tool deltas documented) | `auto:free` → currently `deepseek/deepseek-v4-flash-0731free:free`, `qwen/qwen3.7-flash:free` | `https://api.bazaarlink.ai/v1` (legacy `bazaarlink.ai/api/v1`) | Only the `auto:free` route is free; direct ids are paid. Content not stored by default. |
| 11 | **OrcaRouter** | "Hacker" tier free forever, **rate-capped, numbers unpublished**; 429 + `Retry-After` | account-wide + per model | Y / Y | `orcarouter/free` (difficulty router), `deepseek/deepseek-v4-flash-free`, `z-ai/glm-5.3-flash-free`, `tencent/hy3-free` | `https://api.orcarouter.ai/v1` | GitHub OAuth signup. `api.orcarouter.ai` is a CNAME into Tencent EdgeOne that failed on one resolver during this check: a resolver-independence test belongs in the plugin's doctor probe. Free routes never fall back to paid, so a 429 is a clean quota signal. |
| 12 | **UnoRouter** | **1 request/min per free model per user**; some pools add daily token budgets (amounts n/p, reset 00:00 UTC) | per model | per-model badge / ? | `gpt-oss-120b:free`, `ling-3.1-flash:free`, `mimo-v2.6-flash:free` | `https://api.unorouter.com/v1` | A burst trips an account-wide 429 for minutes; 413 on prompts over ~8K tokens, which rules it out for Claude Code bodies. Plain-chat rung only. |
| 13 | **LLM7.io** | Free token: **60 req/min, 250 req/hr, 100,000 tokens per rolling 24 h** | account-wide | L (catalog `tools_calling: true` on turbo tier; terms list function calling under Pro) / Y | `gpt-oss:20b`, `GLM-5.3-Flash`, `DeepSeek-V4-Flash-0731`, `minimax-m2.7`, `codestral-latest` | `https://api.llm7.io/v1` | Anonymous access exists but limits are undocumented. Prompts used only to operate/improve; responses cached 5 min. |
| 14 | **AnyAPI** | **100K tokens/day**, resets every 24 h, no rollover | account-wide | Y / Y | `qwen-qwen3-8-27b-free`, `google-gemma-3n-4b-free`, two `inclusionai-ling-3-0-flash-*-free` | `https://api.anyapi.ai/v1` | "Doesn't store prompts or responses"; accepts a `training_allowed: false` request flag. Small models. |
| 15 | **Typhoon (OpenTyphoon, AI Singapore-adjacent Thai lab)** | Free research tier, "zero cost for light usage" | per model | Y / Y | `typhoon-v2.5-30b-a3b-instruct` (128K), `typhoon-v2.1-12b-instruct` | `https://api.opentyphoon.ai/v1` | **5 req/s, 200 req/min**, no daily cap stated. Terms grant Typhoon a perpetual right to use inputs for training. Thai/English focus. |
| 16 | **Speka** | **$1 of model usage/month** on Free | account-wide | Y / Y | Llama 3.1 8B, Llama 3.2 3B, GPT-OSS 20B; embeddings | `https://speka.me/v1` | 10 req/min; 402 when the allowance is spent. No training on prompts. Public `/models`, so validate via a deliberately nonexistent model id (400 after auth). |
| 17 | **LongCat (Meituan)** | Recurring daily quota; official docs do not quantify it (third-party: 500K/day default, up to 5M/day on request) | account-wide | ? (not documented; "agentic" models) / Y | `LongCat-2.5-Preview`, `LongCat-Flash-Chat`, `LongCat-Flash-Lite` | `https://api.longcat.chat/openai/v1` (also Anthropic wire at `/anthropic/v1/messages`) | The one Chinese platform with **documented overseas email signup and no ID check**. 429 carries `retry_after: 60`. 1M context. |
| 18 | **Logfare** | Fixed UTC windows: **20/min, 500/hr, 2,500/day, 3 concurrent** (first 24 h: 5/100/250, 1 concurrent) | account-wide | ? / Y | `logfare/auto`, `gemma-4-26b`; premium tier (opt-in screening) adds `deepseek-v4.1-flash`, `qwen-3.8-27b` | `https://logfare.ai/v1` | **All request bodies are logged** and used for internal eval sets after PII scrubbing; Discord-linked registration. Opt-in only, with a warning. |
| 19 | **Agnes AI** | Free plan **20 RPM**; per-token price not published (served at $0 as of mid-2026, promotional) | account-wide | Y / Y | `agnes-2.5-flash` (512K ctx), `agnes-2.0-flash` | `https://apihub.agnes-ai.com/v1` | Only readable official source is the company GitHub README; platform site is a JS shell. Reasoning-before-answer, 20 s TTFB. |
| 20 | **SEA-LION (AI Singapore)** | **10 req/min per user**, one key per user | account-wide | Y / ? (streaming not documented) | `aisingapore/Qwen-SEA-LION-v4.5-27B-IT`, `aisingapore/Llama-SEA-LION-v3.5-70B-R` | `https://api.sea-lion.ai/v1` | Google sign-in. Government-backed, stable. SEA-language strength, average coding. |
| 21 | **Inferbase** | **5,000 req/month**, managed inference **100 req/day and $0.50/day**; 8,192 in / 1,024 out per managed request | account-wide | Y / Y | `auto`, `deepseek-v3`, `deepseek-r1-0528` under the daily allowance | `https://api.inferbase.ai/v1` | 1,024 output cap disqualifies it for code generation. Validate on `/api/v1/inference/credits` ($0 balance is valid). Interleaves billing receipts into the SSE stream, which a strict parser must drop. |
| 22 | **Router9** | **10,000 credits/month (~$1)**, free-tier models only; rate limits n/p | account-wide | ? / ? | `deepseek/deepseek-v4-flash`, `z-ai/glm-5.2`, `qwen/qwen3-30b-a3b-instruct-2507` | `https://api.router9.com/v1` | Docs page 404s. Low confidence. |
| 23 | **CLōD** | **100 req/day, auto-replenished** (1,000/day after a one-time $25 purchase) | account-wide | ? | "8 free open-source models", unnamed outside the app | `https://api.clod.io/v1` (host answers 401; URL not in docs) | Cloudflare UA challenge; accepts friendly names with spaces and reports a namespaced id back. No training on inputs per ToS. |

### 4.4 Bucket D: skip, with reasons

| Provider | Reason |
|---|---|
| **GitHub Models** | **Retired 2026-07-30**: "the playground, model catalog, inference API, and BYOK are no longer available to any customer." |
| **Cerebras** | Card required; one-time $5, 30-day expiry (§2). Keep the plugin for users who are inside the trial, stop advertising it as free. |
| **HuggingFace Inference Providers (Free)** | No monthly credit for free accounts (§2). PRO at $2/month is paid. Keep the plugin for PRO users. |
| **Cohere** | ToS forbids personal/household use; API data may be used for product improvement. |
| **Reka** | No free tier for new accounts; prepaid credits required. |
| **SiliconFlow (international)** | No $0 chat models; one-time $1 credit. The mainland site's free Qwen tier is behind PRC signup and was unreachable. |
| **Pollinations** | Text catalog no longer has $0 models; keyless "classic" path is 1 request per 30 s per IP; free "pollen" amounts undocumented. |
| **AINative Studio** | No free tier (3-day trial then $5/month); the "10M tokens/month" claim appears nowhere official. |
| **SimpleLLM** | Only one 0-cost model (`gemma-4-E4B`); rate-limit page 404s. |
| **Experiential Labs** | Card verification required ($1 charge). |
| **Sail Research** | $5/month credit only while a payment method is attached; background-job Responses API only. |
| **Aclide** | No `chat/completions` at all (Responses and Anthropic Messages only). Possible later via FreeRide's Anthropic translator, not now. |
| **Plugsky** | Function calling "locked until you upgrade". |
| **Gizmo Platforms** | "Tool calls are not supported by this endpoint." |
| **Moondream** | Vision-only VLM, no tools; $5/month credit. Out of scope. |
| **AI Horde** | No tool calling (see §4.2). |
| **NavyAI, NaraRouter** | Hosts reset TLS from outside their allow-list; nothing verifiable beyond a Discord listing. |
| **B.AI, AMD Radeon Cloud TokenFactory** | No readable official page (JS-only shell / DNS fails outside PRC). Unverifiable. |
| **BlazeAPI** | Site is a JS shell; every `free/*` model was `provider_currently_down` at check time. |
| **LLMTR** | Zero-price routes are time-boxed promotions (current one ends 2026-11-02, then HTTP 410); Turkish-only console. |
| **ModelScope, Baidu Qianfan, Volcengine Ark, iFlytek Spark** | Generous quotas (ModelScope ~2,000 req/day; Volcengine 2M tokens/day/model) but all require PRC real-name verification (Alipay/face-scan or PRC ID) with no documented passport path; iFlytek's free Lite model also has no tool calling. Document as "works if you have a verified mainland account", do not ship in `freeride init`. |
| **Tavily, Exa, Firecrawl, Linkup, Parallel** | Search APIs, not chat. |

---

## 5. What FreeRide needs in order to absorb these

Today every provider costs about 260-400 lines of near-identical Python
(`freeride/providers/cerebras.py` is 263 lines, of which perhaps 30 are Cerebras-specific)
plus edits in four places: `freeride/core/provider_env.py` (`BUILTIN_PROVIDERS`),
`freeride/cli/cmd_init.py` (`PROVIDERS`), `freeride/cli/cmd_serve.py`
(`build_provider_registry`), and `tests/conformance/test_provider_conformance.py`.
That is fine for seven providers and wrong for thirty.

### 5.1 A declarative OpenAI-compatible provider

Add `freeride/providers/openai_compat.py`: one class implementing the `Provider` Protocol,
parameterised by a frozen spec:

```python
@dataclass(frozen=True)
class OpenAICompatSpec:
    name: str                      # plugin id, e.g. "mistral"
    base_url: str                  # ".../v1"
    env_vars: tuple[str, ...]      # ("MISTRAL_API_KEY",)
    extra_required: tuple[str, ...] = ()
    keyless: bool = False          # OVH anon, Kilo, BlockRun
    extra_headers: Mapping[str, str] = {}   # UA for Routeway/Septor/CLōD; referer/title
    timeout_s: float = 30.0        # 90 for GLM Flash / Agnes / Ollama reasoning models
    first_byte_s: float | None = None
    validate: Literal["models", "nonexistent-model", "custom"] = "models"
    validate_url: str | None = None          # /v1/usage, /v1/me, /v1/user/me, ...
    force_single_tool_call: bool = False     # NIM-style upstreams
    free_filter: Callable[[dict], bool] | None = None   # ":free" suffix, access_tier, price == 0
    metadata: Mapping[str, ModelMeta] = {}   # context_length, supported_parameters ("tools",)
    pool: Literal["account", "model"] = "account"  # how the free allowance is shared
    daily_requests: int | None = None        # published cap, for pre-emptive pacing
    daily_tokens: int | None = None
    rpm: int | None = None
    data_use: Literal["none", "operate", "train"] = "operate"
    default_on: bool = True        # False for anything with data_use == "train" or keyless
```

The existing Groq, Cerebras, HuggingFace and Ollama plugins can be re-expressed as specs
with no behaviour change; OpenRouter, NIM and Cloudflare keep their bespoke classes.
A single `PROVIDER_SPECS` table then drives `provider_env`, `cmd_init`, `cmd_serve` and the
conformance suite, so adding a provider becomes one table row plus a metadata sidecar.

### 5.2 Keyless mode

`is_configured()` requires a key. Add `keyless=True` specs that are configured by an explicit
opt-in (`FREERIDE_KEYLESS=ovh,kilo,blockrun` or a `freeride init --keyless` flag), never by
default. The README's privacy promise is about FreeRide's beacon; keyless gateways are third
parties that may log prompts, and the init wizard must say so per provider (Kilo trains;
Logfare logs everything).

### 5.3 Account-wide and per-model quota pools

FreeRide cools down per `(provider, key)` after a 429. That is right for OpenRouter
(account-wide daily cap) and wrong for SambaNova (20 RPD **per model**), UnoRouter
(1 rpm per model) and OVH (2 rpm per model): a 429 on one model says nothing about the
others. The spec's `pool` field should select whether `KeyCooldown` keys on `(provider, key)`
or `(provider, key, model)`. The ladder should also prefer spreading across models of a
per-model provider instead of retrying the same id.

Where a cap is published (`daily_requests`, `daily_tokens`, `rpm`), keep a local sliding
counter and skip the provider before it 429s. This is what stops a 70-tool Claude Code turn
from eating a 20-request daily allowance in one go.

### 5.4 Retry hints from error bodies

`retry_after_hint()` only reads the `Retry-After` header. Several candidates state the delay
in the body instead: Gemini's 429 carries `error.details[].retryDelay` ("17s"), LongCat sends
`retry_after: 60`, Groq and others write "try again in 30 s" prose. Parse structured fields
depth-first and anchored prose, clamp to 24 h, keep only the number.

### 5.5 Validation must not spend metered quota

`freeride doctor`/`keys` probe keys periodically. Rules the spec must encode:

- Never treat a public `GET /v1/models` 200 as proof of a valid key (xKiro, Speka, Typhoon,
  ElectronHub, Pollinations all answer 200 unauthenticated).
- Prefer a free authenticated read (`/v1/usage`, `/v1/me`, `/v1/user/me`, `/credits`) or a
  chat call against a deliberately nonexistent model, which fails after auth without
  generating tokens.
- A $0 balance is a valid key on credit-wallet gateways (Speka, Inferbase).
- Some gateways answer validation probes with 403 "automated validation not supported":
  inconclusive, never a disable.

### 5.6 Tool capability is a per-model fact, verified live

The agent ladder admits only models whose `Model.supported_parameters` contains `"tools"`
(`freeride/server/routes/fx.py`). Most gateways do not expose a capability field, and the
ones that do (Kilo `supported_parameters`, Requesty `supports_tool_calling`, xKiro
`capabilities.tools`, Septor "Tools" tag) are self-reported. Extend `freeride audit-models`
with a tool-call smoke test (one `get_weather` function, assert a `tool_calls` block comes
back) and persist the verdict in `model_health.json` alongside the existing probe. Models
that never emit `tool_calls` get `supported_parameters=()` regardless of what the catalog
says. This also catches the gpt-oss "answers in text instead of calling the tool" failure the
Claude Code pin comment in `messages.py` already documents.

### 5.7 Response model must equal requested model

Four gateways were observed silently substituting a different model under HTTP 200
(BlockRun, Septor, Lucidity `open/*`, CLōD Llama→Gemma). For a coding agent that is a
correctness bug, not a quirk. The generic adapter compares `response.model` (normalised:
strip `:free`, provider prefixes, case) against the requested id; with
`strict_model_match` the mismatch is classified as `MODEL_NOT_FOUND` so the ladder moves on.
Routers that legitimately report the upstream id (`auto` routes on DreamPrompting,
BazaarLink, OrcaRouter, Waterfall) are exempt by route name.

### 5.8 Gemini specifics

Use the OpenAI-compatible endpoint, not the native wire format. Two things to handle in a
thin subclass: cache `thoughtSignature` values from tool calls keyed by tool-call id (and by
a name+arguments fingerprint, since agents rewrite ids) and replay them, falling back to
Google's documented sentinel when absent; and strip the JSON-Schema keywords Gemini's
validator rejects (`$schema`, `additionalProperties` in some positions, `strict`). Region
gating per the terms (EEA/UK/CH paid-only) belongs in `freeride init`.

### 5.9 Catalog as data, refreshed out of band

Free rosters rotate weekly on the aggregators. Keeping model ids in Python means a PyPI
release per rotation. Move per-provider free-model allowlists and metadata into a JSON
catalog shipped in the wheel, and let `freeride audit-models` write a refreshed local copy
under `~/.freeride/cache/`. A later step can fetch a signed catalog from free-ride.xyz on the
same hourly cadence as the telemetry beacon, which keeps the gateway correct between
releases without any server-side inference.

---

## 6. Rollout plan

| Phase | Scope | Effort | Exit criterion |
|---|---|---|---|
| **0. Corrections** | Fix README/init copy for Cerebras, HuggingFace, NIM; state OpenRouter's 50/day cap. | hours | README matches provider pages. |
| **1. Generic adapter** | `openai_compat.py` + `PROVIDER_SPECS`; port Groq, Cerebras, HF, Ollama to specs; `pool`, `daily_*`, body retry hints, response-model check, quota-safe validation. Conformance suite runs every spec. | 3-4 days | 878+ tests green; existing seven providers behave identically. |
| **2. Bucket A** | Mistral, SambaNova, Z.ai, Ollama Cloud as specs; Gemini as a thin subclass. Init wizard rows, docs/providers/*.md per provider, tool-call smoke test in audit-models. | 4-5 days | Live probe shows tool calls from at least one free model on each; ladder preset order updated (`PRESET_CODING`: openrouter, gemini, mistral, sambanova, groq). |
| **3. Bucket B keyless** | OVH, Kilo, BlockRun behind `FREERIDE_KEYLESS` opt-in with per-provider data-use warning. | 1 day | A fresh install with zero keys answers `freeride/free` through OVH. |
| **4. Bucket C** | Add the top ~10 of §4.3 as spec rows (xKiro, Requesty, Routeway, Septor, ElectronHub, Lucidity, DreamPrompting, Waterfall, LLM7, LongCat); the rest as community-contributable rows with a template PR. | 2 days | Each row has a passing conformance test and a probe log in its doc page. |
| **5. Catalog as data** | JSON catalog, local refresh, optional signed remote refresh. | 2-3 days | A free-model rotation is handled without a release. |

Keep `ridex`'s `assets/skills/freeride/SKILL.md` and the picker catalog in sync at phase 2,
since the TUI lists providers by name.

---

## 7. Risks

- **Churn.** Six providers changed their free terms in the last five months (Cerebras,
  HuggingFace, GitHub, SambaNova, OpenCode, OpenRouter's cap tiering). Anything not verified by
  an automated probe will drift; phase 5 exists for this reason.
- **Privacy posture.** FreeRide's pitch is "your machine talks to providers directly". Adding
  gateways that are themselves proxies (DreamPrompting, Waterfall, every bucket-C router)
  weakens that: prompts now pass through a third party before reaching the model host. Make
  the spec's `data_use` field visible in `freeride keys` and the init wizard.
- **ToS.** Gemini's "professional or business purposes" clause, NIM's evaluation-only terms,
  and Z.ai's anti-redirect clause are all defensible for a personal single-user proxy and all
  new in 2026. Keep the existing "one account per provider, no resale, no sharing" guidance.
- **Reputation.** Sending a product User-Agent (needed for Routeway, Septor, CLōD) and the
  OpenRouter attribution headers means free-tier abuse by one user is attributable to FreeRide.
  The per-install UUID in the beacon is not sent upstream; keep it that way.
- **Health probes as load.** Thirty providers × a 5-minute validation cadence is 8,640
  probes/day. Validate on demand (`freeride keys`, startup, after a 401), not on a timer.

---

## Appendix A. Source index

First-party: ai.google.dev/gemini-api/docs/{rate-limits,pricing,openai}, ai.google.dev/gemini-api/terms ·
docs.mistral.ai/admin/billing-usage/{usage-limits,subscriptions}, docs.mistral.ai/api ·
docs.sambanova.ai/docs/en/models/rate-limits, docs.sambanova.ai/docs/en/features/openai-compatibility ·
ollama.com/pricing, docs.ollama.com/api/openai-compatibility, docs.ollama.com/cloud ·
docs.z.ai/guides/overview/{pricing,quick-start}, docs.z.ai/guides/capabilities/function-calling ·
docs.cohere.com/docs/{rate-limits,compatibility-api}, cohere.com/terms-of-use ·
build.nvidia.com model pages · console.groq.com/docs/{rate-limits,tool-use} ·
cerebras.ai/pricing, inference-docs.cerebras.ai/support/rate-limits ·
github.blog/changelog/2026-07-30-github-models-is-now-retired ·
huggingface.co/docs/inference-providers/pricing · developers.cloudflare.com/workers-ai/platform/{pricing,limits} ·
openrouter.ai/docs/api-reference/limits · opencode.ai/docs/zen.

Keyless/community: docs.ovhcloud.com (ai-endpoints getting-started, function-calling), ovhcloud.com/en/public-cloud/ai-endpoints/catalog ·
kilo.ai/docs/gateway/{authentication,usage-and-billing}, ai-gateway.kilo.ai/api/v1/models ·
blockrun.ai/{pricing,docs} · oai.aihorde.net openapi.json, aihorde.net/api/v2/status/models ·
gen.pollinations.ai/docs/llm.txt · docs.llm7.io/limits, llm7.io TERMS.

Aggregators: xkiro.com, docs.xkiro.com/api/{overview,rate-limits} · requesty.ai/{pricing,free-models}, docs.requesty.ai ·
routeway.ai, docs.routeway.ai · septorlabs.com/{pricing,models,docs/rate-limits} ·
docs.electronhub.ai/billing/credits, docs.electronhub.ai/getting-started/rate-limits ·
composite.lucidity.sh/docs · dreamprompting.com/{pricing,api-docs} · getwaterfall.org/{pricing,docs,trust} ·
api.airforce/{pricing,docs,models} · bazaarlink.ai/docs · orcarouter.ai/{pricing,models}, docs.orcarouter.ai ·
unorouter.com/en/docs/platform/errors-and-rate-limits · anyapi.ai/pricing, docs.anyapi.ai ·
docs.opentyphoon.ai/en/{rate-limits,tool}, opentyphoon.ai/tac · speka.me/{pricing,docs} ·
longcat.chat/platform/docs (Quick Start, FAQ) · logfare.ai/{docs,tos} · github.com/AgnesAI-Labs/AgnesAI-Models ·
docs.sea-lion.ai/guides/inferencing/api · inferbase.ai/{pricing,docs/api} · router9.com/{pricing,models} · clod.io/{pricing,terms}.

Skipped: experientiallabs.ai/pricing · docs.sailresearch.com/pricing · aclide.com/en/pricing · plugsky.com/docs ·
gizmoplatforms.com/developers · docs.moondream.ai · docs.reka.ai · siliconflow.com/pricing · ainative.studio/pricing ·
simplellm.eu/docs · llmtr.com/docs · api.blazeapi.org/{paid,free}/v1/models ·
ModelScope / Qianfan / Volcengine / iFlytek official docs where reachable, otherwise dated third-party trackers (labelled in §4.4).

## Appendix B. Glossary of recurring quirks

| Quirk | Seen at | Handling |
|---|---|---|
| Public `/v1/models` answers 200 without auth | xKiro, Speka, Typhoon, ElectronHub, Pollinations, UnoRouter | validate elsewhere (§5.5) |
| Cloudflare challenge on non-browser User-Agent | Routeway, Septor, CLōD | `extra_headers` with a product UA |
| Silent model substitution under HTTP 200 | Septor, Lucidity `open/*`, CLōD | response-model check (§5.7) |
| Hidden reasoning, 20-60 s TTFB | GLM Flash, Agnes, Ollama Cloud frontier models, NIM reasoning models | `first_byte_s` 90-180 |
| Per-model (not account) quota | SambaNova, OVH, UnoRouter, Typhoon | `pool="model"` |
| Billing/routing frames interleaved in SSE | Inferbase | drop non-OpenAI chunks |
| HTTP-200 body carrying an error banner | ElectronHub | sniff leading "error" heading |
| 413 on large bodies | UnoRouter (~8K tokens), Groq 70B (historical) | exclude from Claude Code pin |
| Parallel tool calls rejected | NIM, OVH Llama | `force_single_tool_call` |
| `thoughtSignature` must round-trip | Gemini 3 | cache + sentinel (§5.8) |
