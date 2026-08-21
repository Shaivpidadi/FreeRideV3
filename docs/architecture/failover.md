# Failover

> How the gateway turns "I have keys for 5 free-tier providers" into "the request succeeds even when 3 of them are throttled."

The failover machinery is what makes FreeRide more than a thin proxy. It's the same code path serving every endpoint — `/v1/chat/completions`, `/v1/messages` (Claude Code), `/v1/responses` (Codex), `/v1beta/models/*:generateContent` (Gemini CLI) — so improvements to it benefit every client uniformly.

## The chain

For each request the gateway builds an ordered **chain of (provider, [keys])**, then walks it.

```
[
  (openrouter,    [or_key_1, or_key_2, or_key_3]),
  (groq,          [groq_key]),
  (nvidia_nim,    [nvidia_key_1, nvidia_key_2]),
  (huggingface,   [hf_key]),
  (cerebras,      [cerebras_key]),
  (cloudflare,    [cf_key]),
  (ollama,        [None]),   # local — no key needed
]
```

Order is **sorted by recent health** (see [Health tracking](#health-tracking) below). Within each provider, keys are also health-sorted — a flaky key gets demoted relative to its siblings without taking the whole provider out of rotation.

## Walking the chain

For each `(provider, keys)` pair:

1. Try the head key on this provider.
2. Classify the response:
   - **`OK`** → done. Stamp `X-FreeRide-Provider: <name>` and ship to the client.
   - **`RATE_LIMIT`** → mark this key as cooling for the rate-limit window (typically 60s). Try the next key on the same provider.
   - **`AUTH`** → key is bad. Cool it and try the next key.
   - **`MODEL_NOT_FOUND`** → this provider doesn't have the requested model. Invalidate the catalog cache (so the next request re-resolves). Skip to the next provider.
   - **`QUOTA_EXHAUSTED`** → daily quota tripped. Cool the key for a longer window (~1h). Skip to next provider.
   - **`TIMEOUT`** or **`5xx`** (provider-side error) → record the failure but don't cool aggressively (transient). Try the next pair.
3. If every pair in the chain has failed, return a structured 503 with the per-provider attempt list:

   ```json
   {
     "type": "error",
     "error": {
       "type": "api_error",
       "message": "All providers failed.",
       "request_id": "req_abc123",
       "tried": [
         {"provider": "openrouter", "last_error": "rate_limit", "keys_tried": 2},
         {"provider": "groq", "last_error": "model_not_found", "keys_tried": 1}
       ],
       "suggestion": "..."
     }
   }
   ```

This is what makes debugging single-log-line. The client / CLI surfaces the structured tried-list so a user sees exactly which providers got attempted and why each failed.

## Mid-stream failures

For streaming requests, the gateway pre-flights the **first chunk** through the failover loop. If no provider produces a first byte without erroring, all the normal failover kicks in. **Once the first chunk has shipped to the client, the gateway is committed** — we can't un-ship bytes. A mid-stream upstream error gets logged but the client sees the stream end early.

This is documented in the routes (`messages.py:626`, `codex.py:340`, `gemini.py:380`) as "buffer-first-chunk semantics."

## Health tracking

A small `ProviderHealth` tracker records per-provider and per-key statistics:

- Recent **success rate** (rolling 50-attempt window)
- **Last failure kind** + timestamp
- **Latency p50** (used by `freeride bench`)
- **Cooldown state** (when this key/provider is in penalty for rate-limit or quota)

`sort_by_health()` and `sort_keys_by_health()` use these stats to bias the chain order. A provider that's been 100% success for the last 50 requests sorts ahead of one that's been 50% successful, even if both are "currently OK."

The tracker lives in memory only (no persistence across restarts). Per-request, recording happens in `_record_health()` called from each route's failover loop.

## Cooldowns

A separate `KeyCooldown` object tracks which provider/key combinations are temporarily unusable:

- **Rate limit hit** → key cools for the duration suggested by the provider's `Retry-After` header, or 60s default.
- **Auth failure** → key cools for 5min (probably bad key, but might be transient).
- **Quota exhausted** → key cools for 60min (likely the daily reset window).

`_resolve_provider_chain()` filters out cooling keys before the failover loop sees them, so a provider with all keys cooling drops out of the chain entirely for the duration. The next request rebuilds the chain fresh.

## Smart-router for `model: "auto"`

When the client sends `model: "auto"` (or one of our `freeride/<preset>` ids that rewrite to auto), the gateway resolves it to a concrete model id before dispatch:

1. Fetch the catalog of free models per provider (cached).
2. Score each model by `health × popularity`, where popularity comes from the public [models leaderboard](https://free-ride.xyz/models) — tokens served across the FreeRide community.
3. Pick the top-scored model on the provider that's currently at the head of the failover chain.

The popularity signal means "models other users find reliable" automatically surface first — the leaderboard reads our telemetry, the smart-router reads the leaderboard, the loop closes. Run `freeride audit-models` once after install to warm the local cache so the first real request isn't a cold start.

Code lives in `freeride/core/auto_model.py` and `freeride/core/smart_routing.py`.

## Force-provider override

For debugging or A/B testing, clients can pin a single provider per request:

```bash
curl -H 'X-FreeRide-Force-Provider: groq' \
     -d '{"model": "auto", "messages": [...]}' \
     http://localhost:11343/v1/chat/completions
```

The chain is filtered to just `groq` before walking. If groq isn't registered or has no usable keys, the request 400s with the registered-provider list. Useful for isolating which provider produced a weird response.

## Code surface

The failover loop is shared across routes:

* `freeride/core/failover.py` — the walk (`try_stream_with_failover`, `try_call_with_failover`), chain construction, 503 builder, health recording.
* `freeride/core/provider_env.py` — the single provider ↔ env-var registry (including `OPENROUTER_API_KEY_2` numbered suffixes).
* `freeride/core/cooldown.py` — hashed per-key cooldowns with TTL by error kind.
* `freeride/server/routes/chat.py` — OpenAI Chat Completions envelope; sibling routes (`messages.py`, `codex.py`, `gemini.py`, `embeddings.py`) keep their translators and call the shared walk.


## Observability

Every transition emits a structured event to `~/.freeride/events.jsonl`. A typical successful request looks like:

```json
{"type": "request_start",          "request_id": "req_abc", "model": "auto", "streaming": true}
{"type": "auto_model_resolved",    "request_id": "req_abc", "resolved_model": "openrouter/owl-alpha"}
{"type": "provider_attempt",       "request_id": "req_abc", "provider": "openrouter", "key_index": 0}
{"type": "provider_response",      "request_id": "req_abc", "provider": "openrouter", "status": "OK", "duration_ms": 3159}
{"type": "request_complete",       "request_id": "req_abc", "provider": "openrouter", "streaming": true}
```

A failure cascade looks like:

```json
{"type": "provider_attempt",       "provider": "openrouter", "key_index": 0}
{"type": "provider_response",      "provider": "openrouter", "status": "rate_limit", "duration_ms": 87}
{"type": "provider_attempt",       "provider": "openrouter", "key_index": 1}
{"type": "provider_response",      "provider": "openrouter", "status": "rate_limit", "duration_ms": 92}
{"type": "provider_attempt",       "provider": "groq", "key_index": 0}
{"type": "provider_response",      "provider": "groq", "status": "OK", "duration_ms": 1240}
{"type": "request_complete",       "provider": "groq"}
```

Tail this file with `tail -f ~/.freeride/events.jsonl` to debug routing decisions in real time.
