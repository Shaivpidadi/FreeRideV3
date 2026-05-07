# Hermes — identification + config

## Issue source

`Shaivpidadi/FreeRide#11` — opened 2026-04-11 by `lewdon`, OPEN, no comments.
Title: `hermes agent support?`
Body (full): `How to modify for hermes agent ?`

The issue gives no link or context. Identification done by external search.

## Identification

**Hermes Agent** by Nous Research — confidently identified.

- Repo: https://github.com/NousResearch/hermes-agent
- Stars: ~136k (top hit by far for `hermes agent`)
- Description: "The agent that grows with you"
- Homepage: https://hermes-agent.nousresearch.com
- Language: Python
- Topics include `openclaw`, confirming same ecosystem niche as the FreeRide v2 host. Multiple companion repos (`hermes-webui`, `hermes-desktop`, `hermes-workspace`, `hermes-agent-self-evolution`, etc.) and a "Cowork" app explicitly bundling "OpenClaw, Hermes Agent, Claude Code, Codex, OpenCode, Gemini CLI" (`iOfficeAI/AionUi`, 23k stars).

Ruled out:
- Nous Research **Hermes models** (e.g. `Hermes-3-Llama-3.1-70B`) — those are LLMs, not agents.
- HermesJS / Hermes JS engine / unrelated `hermes` repos — none have agent semantics or audience overlap with FreeRide.

## Configuration approach

Hermes is **explicitly designed to accept arbitrary OpenAI-compatible base URLs.** No reverse-engineering required.

**Two-layer config:**
1. `~/.hermes/config.yaml` — model + provider + base_url (durable user config)
2. `.env` (project- or home-local) — API keys; takes precedence over yaml for credentials
3. Project-local `cli-config.yaml` — overrides

**Relevant fields** (verbatim from `cli-config.yaml.example` in the repo):

```yaml
model:
  default: "openrouter/free"
  provider: "custom"      # any OpenAI-compatible endpoint
  base_url: "http://localhost:11343/v1"
  # api_key: "your-key-here"   # optional; falls back to env
```

The example explicitly documents `"custom" - Any other OpenAI-compatible endpoint. Set base_url below.` with aliases `ollama`, `vllm`, `llamacpp` all mapping to `custom`. So Hermes' provider abstraction is FreeRide-shaped out of the box.

Other override paths:
- `--provider custom --base-url <url>` CLI flags
- `HERMES_INFERENCE_PROVIDER` env var
- `--model` CLI flag

Hermes also has a first-class `nvidia` provider (NIM) and many others — Hermes is itself a multi-provider router. **FreeRide adds value precisely here:** Hermes' multi-provider config can collapse to a single `provider: custom, base_url: http://localhost:11343/v1` entry, and FreeRide handles the cross-provider free-tier rotation underneath.

## FreeRide bind plan

`freeride bind hermes` writes (atomic, preserves unrelated keys) into `~/.hermes/config.yaml`:

```yaml
model:
  provider: custom
  base_url: http://localhost:11343/v1
  api_key: any   # required-but-unused; gateway ignores it
  default: openrouter/free
```

Out of scope for this binder:
- Don't touch `~/.hermes/.env` — credentials remain user-managed.
- Don't change `provider_routing` or `providers:` overrides — those are user-tuned.
- Don't write `cli-config.yaml` (project-local). Bind is user-global only.

E2E test for the binder (Phase 4 Feature 4.x — addition to current the execution plan):
1. Fresh hermes install with default `~/.hermes/config.yaml`.
2. `freeride bind hermes` → file rewritten with the four keys above; all other keys byte-identical.
3. Start hermes; ask a one-shot question; gateway logs show inbound request.

Resolution maps to the design plan: "if Hermes speaks `OPENAI_API_BASE`, no work needed beyond `freeride bind hermes`." It does (via the `custom` provider), so the answer is the binder. **Do not close the issue with "use a different agent."**

## Open questions

- **Issue reporter's version of Hermes.** Stars and topics make `NousResearch/hermes-agent` the only credible match, but a one-line confirmation comment from `@lewdon` would close it. Suggested reply on issue #11: *"Confirming this is `NousResearch/hermes-agent`? V3 will land a `freeride bind hermes` helper."*
- **Whether to add Hermes to the Phase 4 binder set in the execution plan.** Currently Phase 4 names `openclaw / aider / continue`. Recommend appending `hermes` as Feature 4.8.

## Sources

- https://github.com/NousResearch/hermes-agent — repo
- https://github.com/NousResearch/hermes-agent/blob/main/cli-config.yaml.example — config schema (the `provider: custom` documentation comes from here verbatim)
- https://github.com/NousResearch/hermes-agent/blob/main/.env.example — env-var inventory
- https://github.com/Shaivpidadi/FreeRide/issues/11 — original issue (sparse, one-line)
- https://hermes-agent.nousresearch.com — official homepage
