# tests/ci/ — Daytona-based CI matrix

End-to-end tests that exercise FreeRide against real free-tier providers and
real `api.anthropic.com`, on fresh Linux sandboxes. Each test spins up its
own ephemeral Daytona sandbox, installs FreeRide from a git ref, runs the
phase-specific probes, and tears down.

These are **live** tests. They are **not** a substitute for `pytest` —
they're complementary: `pytest` proves the code is internally correct; these
prove that the code, against the real network state of free providers + the
Anthropic API, produces a usable system end-to-end.

---

## Quick start

```bash
# One-time: tier 3 Daytona key + a small ($1-2) Anthropic prepaid key
cp tests/ci/.env.example tests/ci/.env.local   # if you keep an example
# Or directly:
cat > tests/ci/.env.local <<'EOF'
DAYTONA_API_KEY=dtn_...
ANTHROPIC_API_KEY=sk-ant-api03-...
EOF
chmod 600 tests/ci/.env.local

# Load env and Python's CA bundle (macOS python.org Python 3.13 quirk)
set -a; . tests/ci/.env.local; set +a
export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")

# All 5 phases in parallel — the "did I break anything?" command
python tests/ci/run_full_matrix.py

# Or any single phase
python tests/ci/test_normal_flow.py
python tests/ci/test_per_provider.py
python tests/ci/test_failover.py
python tests/ci/test_binders.py
python tests/ci/test_claude_code.py
```

A full matrix run takes ~3-4 minutes (parallel) and costs:

- Daytona: pennies of compute (~25 sandbox-minutes total)
- Anthropic: ~$0.001 (3 Haiku calls)
- Free providers: a handful of small requests against your existing keys

---

## The phases

| Phase | Script | What it proves |
|---|---|---|
| A | `test_normal_flow.py` | Install on fresh Debian, gateway boots, /health + /v1/models + /v1/chat/completions(model=auto) all return real responses |
| B | `test_per_provider.py` | Each provider serves when forced via `X-FreeRide-Force-Provider` |
| C | `test_failover.py` | Inject an invalid OR key, request still succeeds via failover to another provider |
| D | `test_binders.py` | `freeride bind <agent>` produces valid configs for all 5 supported agents |
| E+F | `test_claude_code.py` | Real `claude --print` works wrapped + unwrapped; `freeride/free|fast|quality` all return text via free providers |

`run_full_matrix.py` runs all 5 concurrently via `ThreadPoolExecutor` and
aggregates results.

`_daytona_lib.py` holds the shared primitives (ephemeral_sandbox context
manager, install_freeride, upload_env, launch_gateway, post_chat).

---

## Expected flakiness — these are live tests, not unit tests

**Passthrough probes (Phase E+F's `baseline`, `passthrough_*`)** should always
pass. They hit real Anthropic with your real API key and Anthropic is
reliable.

**Free-route probes** WILL occasionally fail because they depend on the
current state of free-tier providers, which is not under our control:

| Failure shape | Likely cause |
|---|---|
| HF returns 503 `quota_exhausted` | The user's monthly free HF credit ($0.10/mo) ran out. Wait until next month, upgrade to PRO ($2/mo), or set a different HF key. Confirmed via direct probe: `curl https://router.huggingface.co/v1/chat/completions -H "Authorization: Bearer $HF_TOKEN" ...` returns HTTP 402. |
| Groq/Cerebras/NVIDIA return "unknown" or 503 | Free-tier rate limit. Reset cadence varies (Groq: minutes, others: hourly). Heavy CI usage on a single key can exhaust this. |
| NVIDIA timeout on `google/gemma-3-27b-it` or `qwen/qwen2.5-coder-32b-instruct` | NVIDIA's API lists models they can't always serve. Run `freeride audit-models` periodically to maintain a per-model health cache the smart-router consumes. |
| `freeride/fast` 503 with all-providers-failed | Combined effect of the above — `fast` prefers groq first, and if groq is rate-limited the fallback chain may hit other exhausted providers. Try `freeride/quality` or `freeride/free` instead, or wait 5-10 minutes. |
| Phase A `chat_auto_model` hangs > 60s then returns 500 | An upstream provider is slow or hung. The gateway's request budget is finite; eventually it bubbles up the bare 500. Re-run or run `freeride audit-models` first. |

**Rule of thumb:** if `Phase A` setup steps pass (install, env upload, gateway
health) and **Phase E+F passthrough probes** pass (3 of them), the code is
working. Free-route failures = provider state, not freeride bugs.

---

## Distinguishing freeride bugs from provider state

When a free-route probe fails, decide which it is by checking three things:

1. **Did Phase D pass?** Binders write static config files; failure there
   means freeride code is broken.
2. **Did Phase E+F passthrough probes pass?** They hit Anthropic, which is
   reliable; failure means the wrapper or passthrough is broken.
3. **Does plain `curl` to a known-working free provider succeed?** E.g.,
   ```bash
   curl -sS https://api.groq.com/openai/v1/chat/completions \
     -H "Authorization: Bearer $GROQ_API_KEY" \
     -d '{"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
   ```
   If the provider returns 200 directly but freeride returns 503, that's a
   freeride bug.

---

## Recommended pre-flight: `freeride audit-models`

Before running the CI matrix, populate the per-model health cache so the
smart-router skips known-broken upstream models:

```bash
# Locally (uses your real provider keys, takes ~30s)
freeride audit-models --quiet

# After this, the matrix is more deterministic — `model: "auto"` won't
# pick a model the audit knows is currently failing.
```

The audit writes `~/.freeride/cache/model_health.json` which the
smart-router reads at request time. Stale cache (>24h old) is auto-refreshed.

---

## Local CI credentials file

`tests/ci/.env.local` is gitignored and never committed. Recommended
contents:

```
DAYTONA_API_KEY=dtn_...
ANTHROPIC_API_KEY=sk-ant-api03-...
```

We never echo these in test output. Telemetry events stamp an 8-char
SHA-256 prefix (`auth_fingerprint`) of any auth token rather than the token
itself.

---

## Adding a new phase

Pattern: each phase script imports primitives from `_daytona_lib`, builds a
`PhaseReport`, and exits 0 on success. New phases plug into
`run_full_matrix.py`'s `PHASES` list.

Keep wall time under 5 minutes per phase. Daytona sandbox creation alone is
~1-3s; the bottleneck is whatever your phase does inside.
