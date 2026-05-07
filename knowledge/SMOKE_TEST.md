# FreeRide v0.3.0a2 — smoke test for a fresh user

> **For the tester:** thanks for trying this. The whole thing should take ~10 minutes. Just walk through the steps below and copy-paste any errors into the report block at the bottom. We're looking for "does this just work for someone who isn't us."
>
> **What we're testing:** the full happy path from `pip install` → real prompt through Aider → real response. If anything in here doesn't work, we want to know.

## Prereqs

- Python **3.10 or newer** (`python3 --version`)
- macOS or Linux. Windows untested; if you're on Windows please flag this so we know to skip-or-test-later.
- An OpenRouter free API key from <https://openrouter.ai/keys> (free signup, no credit card)
- ~500MB free disk for a venv + Aider

## 1. Install (one command)

```bash
curl -sSL https://raw.githubusercontent.com/Shaivpidadi/FreeRideV3/main/install.sh | sh
freeride --version
```

**Expected:** `freeride 0.3.0a5` (or higher). Anything else, **report**.

The installer bootstraps `uv` (Astral's Python tool installer) if you don't have it, then drops the `freeride` binary at `~/.local/bin/freeride` with PATH set up. `freeride` works in every shell after that, no venv activation needed.

**If `freeride: command not found` after install:** restart your terminal (the installer added `~/.local/bin` to PATH but already-open shells don't see the change). If still nothing, run:

```bash
~/.local/bin/freeride --version
```

## 2. First-run telemetry banner

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."   # paste your free key
freeride --help
```

**Expected:** A **prominent multi-line banner** prints once before the help text:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FreeRide telemetry: ENABLED (default).

Sent hourly to https://telemetry.free-ride.xyz/v1/beacon (silent on failure):
  installation_id, version, os, tokens_served, request_count,
  providers_active, uptime_hours
...
```

**Action:** read the banner. Do you find it easy to understand and the opt-out command clear? If anything in the banner is confusing, **report**.

```bash
freeride --help     # second run — banner should NOT appear again
```

**Expected:** banner gone on the second invocation. If it re-prints, **report**.

## 3. Start the gateway

```bash
freeride serve
```

**Expected:** prints something like

```
freeride gateway listening on http://127.0.0.1:11343
  providers: openrouter
  point any OpenAI-compatible agent at:
    OPENAI_API_BASE=http://127.0.0.1:11343/v1
    OPENAI_API_KEY=any
```

Leave this running. Open a **second terminal** for the rest of the steps.

## 4. Quick health + chat completion

In the second terminal:

```bash
curl -s http://127.0.0.1:11343/health
# expected: {"ok":true,"version":"0.3.0a2","providers":["openrouter"]}

curl -sX POST http://127.0.0.1:11343/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/free","messages":[{"role":"user","content":"reply with just the word: ok"}],"max_tokens":20}'
```

**Expected:** A JSON response with `"choices":[{"message":{"content":"ok",...}}]` (or similar — the model might say "OK" or "Ok"). Anything that errors out, **report**.

## 5. Real agent test — Aider

Install Aider (the `uv`-based installer skips a numpy build problem with regular pip):

```bash
curl -sLS https://aider.chat/install.sh | sh
~/.local/bin/aider --version
```

Bind it:

```bash
freeride bind aider
# expected output should mention: openai-api-base, openai-api-key, model: openai/openrouter/free
```

Now drive a real one-shot prompt:

```bash
mkdir aider-test && cd aider-test
echo 'print("hello")' > sample.py
git init -q && git add . && git commit -qm init

~/.local/bin/aider \
  --no-show-model-warnings \
  --no-auto-commits \
  --yes-always \
  --no-stream \
  --message "Add a top-line comment that just says hello world" \
  sample.py
```

**Expected:** Aider runs, prints something including either **"Applied edit"** or **"No changes"**, and exits 0. The model's output quality doesn't matter — what matters is that the request round-tripped through the gateway and came back. If aider errors out, says it can't find a model, or hangs longer than 60s, **report**.

## 6. Cleanup

Stop the gateway with Ctrl-C in the first terminal. Delete the venv:

```bash
deactivate
cd ..
rm -rf freeride-test/
```

Optional: also remove `~/.aider*` and `~/.freeride/` if you don't want any FreeRide state lying around.

## Report back

Just paste this back, filled in:

```
PYTHON VERSION:        python --version output
OS / ARCH:             macOS arm64  /  Linux x86_64  /  Windows / etc.
INSTALL OK:            yes / no — error if no
BANNER PRINTED ONCE:   yes / no
GATEWAY HEALTH OK:     yes / no
CHAT COMPLETION OK:    yes / no
AIDER ROUND-TRIP OK:   yes / no
TIME ELAPSED:          ~X minutes

CONFUSING / ROUGH EDGES (anything that needed extra thought):
- 

ANY ERROR OUTPUT (paste verbatim):
- 
```

Anything else — even small UX nits — please flag. We're at v0.3.0**a2** (alpha), this is the moment things should be fixed before they harden.
