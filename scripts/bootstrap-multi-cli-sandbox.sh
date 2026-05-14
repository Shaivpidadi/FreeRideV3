#!/usr/bin/env bash
# Bootstrap a clean test environment for FreeRide's multi-CLI wrappers
# (claude / codex / gemini → freeride gateway).
#
# What this script does:
#   1. Sets up user-local npm prefix so `npm install -g` works without sudo
#      (and adds the prefix's bin to PATH for the current shell + ~/.bashrc).
#   2. Installs the three CLIs that we wrap: claude-code, gemini-cli, codex.
#   3. Installs freeride from the feat/multi-cli-support branch via uv.
#   4. Clears any previously-cached login/auth state for all three CLIs so
#      our wrapper's sentinel credentials are what the CLIs end up using.
#      This avoids the very confusing failure mode where a stale OAuth token
#      from a prior `claude login` (etc.) bypasses our gateway entirely.
#   5. Pre-creates ~/.freeride/.env if it's missing and prints what keys
#      to add (we don't touch it if it already has provider keys).
#   6. Starts `freeride serve` on :11343 in the background.
#
# Use:
#   curl -sSL \
#     https://raw.githubusercontent.com/Shaivpidadi/FreeRideV3/feat/multi-cli-support/scripts/bootstrap-multi-cli-sandbox.sh \
#     | bash
#
# After it finishes, try:
#   freeride run claude --model freeride/coding
#   freeride run gemini --skip-trust
#   freeride run codex
#
# Idempotent — re-running is safe. CLIs already on PATH are skipped.

set -euo pipefail

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }

# ─── 1. npm user-local prefix + PATH ───────────────────────────────────
NPM_PREFIX="$HOME/.npm-global"
mkdir -p "$NPM_PREFIX/bin"

if command -v npm > /dev/null; then
  current_prefix="$(npm config get prefix 2>/dev/null || true)"
  if [ "$current_prefix" != "$NPM_PREFIX" ]; then
    log "configuring npm prefix → $NPM_PREFIX"
    npm config set prefix "$NPM_PREFIX"
  fi
else
  warn "npm not found — install Node.js first (https://nodejs.org). Skipping CLI installs."
fi

export PATH="$NPM_PREFIX/bin:$HOME/.local/bin:$PATH"

# Persist for future shells without duplicating.
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] || continue
  if ! grep -q ".npm-global/bin" "$rc" 2>/dev/null; then
    log "adding PATH export to $rc"
    {
      echo ''
      echo '# freeride multi-cli sandbox bootstrap'
      echo 'export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"'
    } >> "$rc"
  fi
done

# ─── 2. install / update the three CLIs ────────────────────────────────
install_npm_cli() {
  local pkg="$1" bin="$2"
  if command -v "$bin" > /dev/null; then
    log "$bin already on PATH ($(command -v "$bin"))"
    return
  fi
  if ! command -v npm > /dev/null; then
    warn "skipping $pkg — npm unavailable"
    return
  fi
  log "installing $pkg"
  npm install -g "$pkg" >/dev/null 2>&1 || warn "$pkg install failed — see 'npm install -g $pkg' output for details"
}

install_npm_cli "@anthropic-ai/claude-code" "claude"
install_npm_cli "@google/gemini-cli"        "gemini"
install_npm_cli "@openai/codex"             "codex"

# ─── 3. install freeride from feat/multi-cli-support ───────────────────
if command -v uv > /dev/null; then
  log "installing freeride-gateway from feat/multi-cli-support"
  uv tool install --force --reinstall --from \
    "git+https://github.com/Shaivpidadi/FreeRideV3.git@feat/multi-cli-support" \
    freeride-gateway > /dev/null 2>&1 || warn "freeride install via uv failed"
else
  warn "uv not found — install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# ─── 4. clear stale CLI auth/login state ───────────────────────────────
# Each CLI may have cached an OAuth token or API key from a prior run.
# Those bypass our wrapper's env-var injection and route AROUND the
# gateway, which produces deeply confusing test results. We back the
# state up rather than deleting it so a regular user (not a sandbox)
# can restore if they ran this by accident.
backup_if_exists() {
  local path="$1"
  if [ -e "$path" ]; then
    local ts; ts="$(date +%s)"
    log "moving aside $path → $path.pre-freeride.$ts"
    mv "$path" "$path.pre-freeride.$ts" 2>/dev/null || warn "could not back up $path"
  fi
}

backup_if_exists "$HOME/.claude/auth.json"        # claude OAuth from `claude login`
backup_if_exists "$HOME/.codex/auth.json"         # codex OAuth from `codex login`
backup_if_exists "$HOME/.codex/config.toml"       # codex config — wrapper passes -c flags
backup_if_exists "$HOME/.gemini/oauth_creds.json" # gemini OAuth from `gemini login`

# ─── 5. provider keys ──────────────────────────────────────────────────
mkdir -p "$HOME/.freeride"
ENV_FILE="$HOME/.freeride/.env"
if [ ! -f "$ENV_FILE" ] || ! grep -q '_API_KEY=' "$ENV_FILE" 2>/dev/null; then
  cat > "$ENV_FILE.template" <<'EOF'
# FreeRide provider API keys. Add the providers you have access to —
# OpenRouter alone is enough to make `freeride run claude/gemini/codex` work.
#
# Get free OpenRouter access: https://openrouter.ai/keys
OPENROUTER_API_KEY=sk-or-v1-replace_me

# Optional — add any of these for more failover headroom:
# GROQ_API_KEY=gsk_replace_me
# NVIDIA_API_KEY=nvapi-replace_me
# HUGGINGFACE_API_KEY=hf_replace_me
# CEREBRAS_API_KEY=replace_me
EOF
  warn "no provider keys configured yet"
  warn "template written to $ENV_FILE.template — add at least OPENROUTER_API_KEY:"
  warn "  cp $ENV_FILE.template $ENV_FILE"
  warn "  vi $ENV_FILE  # paste your keys"
else
  log "provider keys already present in $ENV_FILE"
fi

# ─── 6. start the gateway ──────────────────────────────────────────────
PORT="${FREERIDE_PORT:-11343}"
if curl -fsS "http://localhost:$PORT/health" > /dev/null 2>&1; then
  log "gateway already running on :$PORT"
else
  if command -v freeride > /dev/null; then
    log "starting freeride serve on :$PORT (logs → $HOME/.freeride/serve.log)"
    nohup freeride serve --port "$PORT" > "$HOME/.freeride/serve.log" 2>&1 &
    disown || true
    for _ in $(seq 1 15); do
      sleep 1
      curl -fsS "http://localhost:$PORT/health" > /dev/null 2>&1 && break
    done
    if ! curl -fsS "http://localhost:$PORT/health" > /dev/null 2>&1; then
      warn "gateway didn't come up after 15s — check $HOME/.freeride/serve.log"
    fi
  else
    warn "freeride command not on PATH — skipping gateway start"
  fi
fi

# ─── done ───────────────────────────────────────────────────────────────
echo
log "installed versions:"
command -v freeride > /dev/null && printf "  freeride  %s\n" "$(freeride --version 2>&1 | tail -1)"
command -v claude   > /dev/null && printf "  claude    %s\n" "$(claude --version 2>&1 | head -1)"
command -v gemini   > /dev/null && printf "  gemini    %s\n" "$(gemini --version 2>&1 | head -1)"
command -v codex    > /dev/null && printf "  codex     %s\n" "$(codex --version 2>&1 | head -1)"

cat <<'NEXT'

Ready. Try the wrappers (in a NEW shell or after `exec $SHELL`):

  freeride run claude --model freeride/coding
  freeride run gemini --skip-trust
  freeride run codex

Watch gateway events (in a second shell):

  tail -f ~/.freeride/events.jsonl
NEXT
