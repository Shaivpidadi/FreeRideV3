# Using Claude Code with FreeRide

> FreeRide is a **support layer** for Claude Code, not a replacement. Your Pro/Max subscription keeps working exactly as before. FreeRide steps in only when you explicitly opt in for a session — and inside that session, you can switch between your subscription and free providers per request.

---

## TL;DR

```bash
# install (once)
curl -sSL https://api.free-ride.xyz/install.sh | sh
freeride init                 # collects free-tier provider keys

# any time you want free models available alongside Claude
freeride run claude           # same Claude UI, /model freeride/* now works

# plain `claude` is untouched — uses your subscription, no FreeRide in the path
```

Inside the wrapped Claude session:

```
> /model claude-opus-4-7          # your subscription answers
> /model freeride/free            # free providers answer (smart router)
> /model freeride/fast            # free providers, prefers groq (sub-100ms TTFT)
> /model freeride/quality         # free providers, prefers openrouter
> /model freeride/coding          # free providers, code-tuned models first
```

You flip per request. Conversation history is preserved across switches because Claude Code manages it client-side.

---

## What `freeride run claude` actually does

1. Probes `http://localhost:11343/health`. If the gateway isn't up, autospawns `freeride serve` in the background (log at `~/.freeride/autospawn.log`).
2. Sets `ANTHROPIC_BASE_URL=http://localhost:11343` for the **child process only**. Your shell environment is untouched.
3. Sets `FREERIDE_ACTIVE=1` so the child (and the `freeride doctor --claude-code` probe) can detect "we're inside the wrapper."
4. Passes through `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` if you have them — never fabricates or modifies them.
5. `execvpe`s into `claude`. From this point on the wrapper is out of the way; Ctrl-C, signals, and exit codes behave exactly as without the wrapper.

Outside the wrapper, plain `claude` hits `api.anthropic.com` directly. No magic. No system-wide change. No `/etc/hosts` mutation. Reversible by not typing `freeride run`.

---

## Model id reference

| Model id                | Mode         | Auth required | Behavior |
|-------------------------|--------------|---------------|----------|
| `claude-opus-4-7`       | passthrough  | yes (sub or API key) | Relayed verbatim to `api.anthropic.com`. Your subscription pays. Tools, streaming, beta features all work. |
| `claude-sonnet-4-6`     | passthrough  | yes           | Same as above. |
| `claude-haiku-4-5`      | passthrough  | yes           | Same as above. |
| any other `claude-*`    | passthrough  | yes           | Permissive prefix — future Claude models work without a FreeRide upgrade. |
| `claude-*` (no auth)    | free fallback| no            | If you point at FreeRide without an Anthropic credential, `claude-*` ids degrade to free providers. Lets the gateway be useful without a subscription. |
| `freeride/free`         | free         | no            | Smart router picks the best free model per request via health-ranked failover across every provider you have a key for. |
| `freeride/fast`         | free         | no            | Prefers `groq` → `cerebras` → `nvidia_nim`. LPU / dedicated silicon. Sub-100ms TTFT in practice. |
| `freeride/quality`      | free         | no            | Prefers `openrouter` → `huggingface` → `groq`. Widest free-model catalog. |
| `freeride/coding`       | free         | no            | Prefers `openrouter` → `groq`. Routes through Qwen-Coder, DeepSeek free tiers first. |

The free presets are hints, not contracts. If a preferred provider is rate-limited or offline, failover walks the chain.

---

## Subscription passthrough — how it works

When the model id is `claude-*` AND the request has an `Authorization: Bearer ...` or `x-api-key: ...` header, FreeRide relays the raw bytes to `api.anthropic.com` unchanged. Your subscription's OAuth token (set by `claude login`) or API key is forwarded as-is. We never parse the body, never re-serialize, never strip Anthropic-specific fields. The response comes back untouched.

What this means:

- **Your subscription is the source of truth for billing.** FreeRide never spends a token on your behalf when you use a `claude-*` model id.
- **All Claude features work**: extended thinking, tool use, cache control, beta extensions (`anthropic-beta` header is forwarded).
- **Tokens are never logged.** Telemetry records a non-reversible 8-char SHA-256 prefix (`auth_fingerprint`) so we can debug "same credential across requests" without ever capturing the credential.
- **Errors mirror Anthropic.** A 401 from Anthropic shows up as a 401 with their exact error envelope and `request_id`. You debug it the same way you would without FreeRide in the path.

---

## Free presets — what happens to your `tools` array

Claude Code 2.x sends ~70 tools (`Agent`, `Read`, `Write`, `Bash`, `Edit`, …) on every request. Free providers can't handle that — too many tools, request body too large.

When you pick `/model freeride/*`, FreeRide **strips the `tools` array** before dispatching to free providers. You get a clean text response. You lose tool-using capability for that request.

This is intentional. The whole point of opting into a free preset is "give me a quick text answer." If you need the agentic flow, switch back to `/model claude-*` — passthrough preserves everything.

Telemetry confirms this happens via the `messages_free_tools_stripped` event (with `n_dropped` count).

---

## `claude --print` + tool permissions (gotcha)

This bites everyone, FreeRide or not — including plain `claude --print` against `api.anthropic.com`.

By default, Claude Code prompts the user before invoking destructive tools (`write_file`, `bash`, etc.). In `--print` mode there's no interactive prompt, so the tool gets denied and Claude falls back to printing markdown (e.g., the code in ```python fences instead of writing the file).

For scripted / CI use, opt out of the permission prompts:

```bash
freeride run claude --dangerously-skip-permissions --print "write a python module that ..."
```

The flag is intrinsic to Claude Code; the wrapper just forwards it. Verify the exact same behavior with `claude --print ...` (no `freeride run` prefix) to confirm it's a CC behavior, not a FreeRide bug.

---

## Health check: `freeride doctor --claude-code`

Diagnose the integration in one command:

```bash
freeride doctor --claude-code
```

Adds five Claude-Code-specific probes on top of the default checks:

1. **`FREERIDE_ACTIVE` marker** — are you inside `freeride run`?
2. **`ANTHROPIC_BASE_URL` check** — is it set, does it point at a reachable gateway, or is it pointing past FreeRide directly at Anthropic?
3. **`claude` CLI presence + version** — known concern is Claude Code 2.x's hardcoded OAuth gate; the doctor surfaces the version so you can debug.
4. **Routing decision sanity** — verifies `claude-*` + auth → passthrough, `freeride/*` → free, `claude-*` + no auth → free fallback.
5. **Live free-route probe** — POSTs a real `/v1/messages` request with `freeride/free` and a tiny prompt. 200 + content = wired correctly. Skipped silently if no gateway is up.

Default `freeride doctor` (without the flag) is unchanged — only the Claude Code section is gated on `--claude-code`.

---

## Telemetry events

Every Claude Code request through FreeRide emits structured events to `~/.freeride/events.jsonl`. Tail it with `freeride watch` or pipe through `jq`. Relevant types:

| Event type                          | When |
|-------------------------------------|------|
| `messages_routing_decision`         | Every request, stamps `mode` (passthrough/free), `preset`, `reason` |
| `messages_preset_applied`           | When a typed preset re-orders the provider chain; stamps `preferred_order` |
| `messages_free_tools_stripped`      | When the free path dropped tools; stamps `n_dropped` |
| `passthrough_start` / `_response`   | When relaying to `api.anthropic.com`; stamps `auth_kind` (`authorization`/`x-api-key`), `auth_fingerprint` (8-char hash, non-reversible), `status` |
| `passthrough_transport_error`       | When the upstream is unreachable; surfaces as a 502 to the client |

No prompt content is ever stored. No auth tokens are ever stored. Telemetry is opt-out via `freeride telemetry off`.

---

## Troubleshooting

**`freeride run claude` exits immediately with "command not found: claude"**
Install Claude Code first: `npm i -g @anthropic-ai/claude-code`. The wrapper assumes `claude` is on `PATH`.

**Gateway autospawn fails ("did not become ready within 8s")**
Check `~/.freeride/autospawn.log` for the failure reason. Common causes: port 11343 already in use by something else, missing Python dependency, import error. Fall back to starting the gateway in the foreground: `freeride serve --port 11343`.

**`/model freeride/free` returns "All providers/keys exhausted"**
Run `freeride keys --no-color` to see which providers have usable keys. Common causes: no `OPENROUTER_API_KEY` set, all keys in cooldown from prior rate-limits (wait 60-120s), or all your free providers happen to be in `freeride/*` preset's `head` but unreachable. The preset preference is biasing the chain; if you want to bypass it use `freeride/free` (no preset preference).

**Passthrough returns 401 with "Invalid bearer token"**
That's Anthropic rejecting your credential, not FreeRide. Anthropic only accepts `Authorization: Bearer ...` for OAuth tokens (from `claude login`) and `x-api-key: ...` for API keys. Claude Code uses the right header for whichever auth flow you're on; if you see this 401, your token has expired or been revoked. Re-run `claude login` or generate a new API key.

**`freeride doctor --claude-code` says the gateway is unreachable but I started it**
The gateway and the doctor probe must agree on the port. Default is `11343`. Override with `--port` on both: `freeride serve --port 9000` and `freeride doctor --claude-code --port 9000`.

**Streaming response shows binary garbage**
Pre-`v0.4.0a4+phase4g` behavior. Update to the latest pre-release: `freeride upgrade`.

---

## Limitations

- **Claude Code 2.x OAuth gate**: CC 2.x makes a hardcoded connection to `160.79.104.10:443` for auth checks before any API call. Setting `ANTHROPIC_API_KEY` in your environment bypasses this. The wrapper doesn't mutate `/etc/hosts` — that's a system-wide change we leave to the user.
- **Free providers don't support tools at scale**. The 70-tool array gets stripped in the free path. If you need tools, switch to `/model claude-*`.
- **Free providers don't support extended thinking**. The `thinking` field in the request is dropped before dispatch.
- **Image / document content blocks** still return 501 from the free route. Passthrough relays them unchanged so subscription users can use vision.

---

## Try it (smoke test)

```bash
freeride run claude --print "what's 2+2? answer with just the number." </dev/null
# expected: 4

freeride run claude --model freeride/free --print "say the single word: alive" </dev/null
# expected: alive (via a free provider, check x-freeride-provider header)
```

If both succeed, the integration is live. The latter never spent a token of your subscription.
