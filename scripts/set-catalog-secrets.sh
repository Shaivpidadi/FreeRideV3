#!/bin/sh
# Pushes the provider keys from FreeRideV3/.env and ~/.freeride/.env, plus the
# catalog admin token from services/telemetry/.dev.vars, into the repo's
# GitHub Actions secrets so .github/workflows/catalog.yml can publish.
# Values are never printed. Run from the FreeRideV3 checkout.
set -e
cd "$(dirname "$0")"
NAMES="OPENROUTER_API_KEY GROQ_API_KEY CEREBRAS_API_KEY NVIDIA_API_KEY HF_TOKEN CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID GEMINI_API_KEY SAMBANOVA_API_KEY OLLAMA_CLOUD_API_KEY XKIRO_API_KEY REQUESTY_API_KEY DREAMPROMPTING_API_KEY LLM7_API_KEY ZAI_API_KEY MISTRAL_API_KEY"
for f in .env "$HOME/.freeride/.env" services/telemetry/.dev.vars; do [ -f "$f" ] && { set -a; . "$f"; set +a; }; done
[ -n "$ADMIN_TOKEN" ] && FREERIDE_ADMIN_TOKEN="$ADMIN_TOKEN"
for n in $NAMES FREERIDE_ADMIN_TOKEN; do
  v=$(eval "printf '%s' \"\$$n\"")
  if [ -n "$v" ]; then printf '%s' "$v" | gh secret set "$n" && echo "set $n"; else echo "skip $n (empty)"; fi
done
