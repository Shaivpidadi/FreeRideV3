# FreeRide v0.3.0a5 — smoke test for a fresh user

> **For the tester:** thanks for trying this. The whole thing should take ~5 minutes. Just walk through the steps and copy-paste any errors into the report block at the bottom. We're looking for "does this just work for someone who isn't us."
>
> **What we're testing:** the full happy path from one-line install → real prompt through Aider → real response. If anything in here doesn't work, we want to know.

## Prereqs

- macOS or Linux. Windows untested; if you're on Windows please flag this so we know to skip-or-test-later.
- An OpenRouter free API key from <https://openrouter.ai/keys> (free signup, no credit card)
- ~500MB free disk

(Python is auto-handled by the installer via `uv` — you don't need to install or pick a Python version.)

## 1. Install (one command)

```bash
curl -sSL https://free-ride.xyz/install.sh | sh
freeride --version
```

**Expected:** `freeride 0.3.0a5` (or higher). Anything else, **report**.

The installer bootstraps `uv` (Astral's Python tool installer) if you don't have it, then drops the `freeride` binary at `~/.local/bin/freeride` with PATH set up. `freeride` works in every new shell after that — no venv activation, no PATH dance.

**If `freeride: command not found` after install:** restart your terminal (the installer adds `~/.local/bin` to PATH but already-open shells don't pick up the change). If still nothing, run:

```bash
~/.local/bin/freeride --version
```

If THAT also fails, **report** and we'll dig in.

## 2. First-run telemetry banner

```bash
freeride --help
```

**Expected:** A **prominent multi-line banner** prints once *before* the help text:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FreeRide telemetry: ENABLED (default).

Sent hourly to https://telemetry.free-ride.xyz/v1/beacon (silent on failure):
  installation_id, version, os, tokens_served, request_count,
  providers_active, uptime_hours

Never sent: prompts, completions, model IDs, API keys, hostname, IP.

  Audit payload:  freeride telemetry
  Opt out:        freeride telemetry off

This banner shows once. Configure under ~/.freeride/config.json.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Action:** read the banner. Do you find it easy to understand, and is the opt-out command clear? If anything in the banner is confusing, **report**.

```bash
freeride --version     # second run — banner should NOT appear again
```

**Expected:** banner gone on the second invocation. If it re-prints, **report**.

## 3. Start the gateway

In one terminal:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."     # paste your free key
freeride serve
```

**Expected output:**

```
freeride gateway listening on http://127.0.0.1:11343
  providers: openrouter
  point any OpenAI-compatible agent at:
    OPENAI_API_BASE=http://127.0.0.1:11343/v1
    OPENAI_API_KEY=any
```

Leave this running. Open a **second terminal** for the rest of the steps.

## 4. Health + chat completion

In the second terminal:

```bash
curl -s http://127.0.0.1:11343/health
# expected: {"ok":true,"version":"0.3.0a5","providers":["openrouter"]}

curl -sX POST http://127.0.0.1:11343/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/free","messages":[{"role":"user","content":"reply with just the word: ok"}],"max_tokens":20}'
```

**Expected:** A JSON response with a `choices` array. The model might say "ok", "OK", "Ok", or even ramble — quality isn't what we're testing. What matters: response came back, no errors. If the curl errors out or the JSON contains an `"error"` field, **report** the full output.

## 5. Real agent test — Aider

Install Aider via its official installer (uses `uv` under the hood — fast, no system Python pollution):

```bash
curl -sLS https://aider.chat/install.sh | sh
~/.local/bin/aider --version
```

Bind it to FreeRide:

```bash
freeride bind aider
# expected output mentions: openai-api-base, openai-api-key, model: openai/openrouter/free
```

Now drive a real one-shot prompt:

```bash
mkdir ~/aider-test && cd ~/aider-test
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

**Expected:** Aider runs (you'll see it print "THINKING" or "Added sample.py to the chat" etc.), then either **"Applied edit"** or **"No changes"**, then exits cleanly. The model's actual edit quality doesn't matter — what matters is that the request round-tripped through the gateway and came back. If Aider errors out, says it can't find a model, or hangs longer than 90s, **report**.

## 6. Cleanup (optional)

Stop the gateway with Ctrl-C in the first terminal.

If you want to fully remove FreeRide afterward:

```bash
uv tool uninstall freeride-gateway     # removes the binary + its venv
rm -rf ~/.freeride/                    # local state (config, stats, install_id)
```

Aider stays installed (you may want to keep it). Removing it: `uv tool uninstall aider-chat`.

## Report back

Just paste this back, filled in:

```
OS / ARCH:             macOS arm64  /  Linux x86_64  /  other
SHELL:                 zsh / bash / fish / etc.
TIME ELAPSED:          ~X minutes

INSTALL OK:                yes / no — error if no
freeride --version OK:     yes / no
BANNER PRINTED ONCE:       yes / no
GATEWAY STARTED:           yes / no
HEALTH ENDPOINT 200:       yes / no
CHAT COMPLETION OK:        yes / no
AIDER ROUND-TRIP OK:       yes / no  (Applied edit / No changes / errored)

CONFUSING / ROUGH EDGES (anything that needed extra thought):
- 

ANY ERROR OUTPUT (paste verbatim):
- 
```

Anything else — even small UX nits — please flag. We're at v0.3.0**a5** (alpha), this is the moment things should be fixed before they harden.
