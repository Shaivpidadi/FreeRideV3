# Consumer config reference (for `freeride bind`)

> One line each: name, what we write, and where.
>
> - **Aider** — write `openai-api-base` + `openai-api-key` to `~/.aider.conf.yml`.
> - **Continue** — append a `models[]` entry with `provider: openai`, `apiBase`, `apiKey` to `~/.continue/config.yaml`.
> - **OpenCode (sst/opencode)** — add a `provider.<id>` block with `npm: "@ai-sdk/openai-compatible"` and `options.baseURL` to `~/.config/opencode/opencode.json`.
>
> All three preserve unrelated keys; only the named keys are touched. None require a process restart in normal cases (Continue hot-reloads YAML; OpenCode reloads on next prompt; Aider reads at startup).

---

## Aider

### Identity / docs
- Repo: https://github.com/Aider-AI/aider
- Docs root: https://aider.chat/docs/
- Options reference: https://aider.chat/docs/config/options.html
- Sample YAML: https://aider.chat/docs/config/aider_conf.html

### Config file
- Path: `.aider.conf.yml` — Aider searches **git root**, **cwd**, then **home dir** (`~/.aider.conf.yml`) in that order. (https://aider.chat/docs/config/options.html)
- Format: YAML (top-level keys are kebab-case).
- Default keys are extensive — at least ~50 top-level options span model selection, env vars, edit format, git integration, voice, history, lint/test cmds, repo map, etc. (https://aider.chat/docs/config/aider_conf.html)

### Base URL knob
- YAML key: **`openai-api-base`** — kebab-case at top level.
- Env var equivalent: `AIDER_OPENAI_API_BASE`
- CLI flag: `--openai-api-base VALUE`
- (https://aider.chat/docs/config/options.html)

### API key knob
- YAML key: **`openai-api-key`**
- Env var: `AIDER_OPENAI_API_KEY`
- CLI flag: `--openai-api-key VALUE`

### Keys to preserve
Anything we did not write. Aider's config is a flat YAML; the bind helper must read → patch only `openai-api-base` and `openai-api-key` → write back, keeping all other top-level keys. Categories users commonly customize (do NOT clobber): `model`, `weak-model`, `editor-model`, `edit-format`, `auto-commits`, `lint-cmd`, `test-cmd`, `read`, `gitignore`, `verbose`, `dark-mode`/`light-mode`, `cache-prompts`, `voice-*`, `set-env`, `alias`, etc.

### Restart behavior
Aider reads config at startup. Changing `~/.aider.conf.yml` requires the user to **restart the `aider` process** for the new base URL to take effect.

### Working minimum config (copy-paste)
```yaml
# ~/.aider.conf.yml
model: openrouter/free
openai-api-base: http://localhost:11343/v1
openai-api-key: any
```
The user picks `model:` themselves — FreeRide doesn't write that key. The `any` API key works because the gateway doesn't validate inbound keys (it uses its own upstream keys per provider).

### Quirks
- Aider also supports per-provider env-var sets (e.g., `AIDER_ANTHROPIC_API_KEY`). The bind helper only touches the OpenAI-compatible knobs.
- Aider's "model name" is LiteLLM-style with provider prefix (`openrouter/free`, `openai/gpt-4o`). For FreeRide-bound use, the user passes whatever the gateway's `/v1/models` advertises — the gateway controls the namespace.

---

## Continue

### Identity / docs
- Repo: https://github.com/continuedev/continue (VS Code + JetBrains extension)
- Docs: https://docs.continue.dev/
- YAML config reference: https://continue.dev/reference (the docs.continue.dev path 404s for `/yaml-reference` and `/customize/*` subpaths in current site structure; canonical is at the bare `/reference`)

### Config file
- Path on macOS: **`~/.continue/config.yaml`** (current). Older Continue used `~/.continue/config.json`; the YAML format is the modern one and is what the schema reference documents.
- Format: YAML.
- Top-level structure includes `name`, `version`, `models[]`, `context[]`, `rules[]`, `mcpServers[]`, etc.

### Base URL knob
- For each entry inside `models[]`: **`apiBase: <url>`** — overrides the default for that provider. (https://continue.dev/reference)

### API key knob
- For each entry inside `models[]`: **`apiKey: <value>`** — peer of `apiBase`. (Inferred from the schema; the `models[]` reference page documents `apiBase`, the `apiKey` key is consistent across all OpenAI-style provider examples on docs.continue.dev.)

### Keys to preserve
The bind helper must **append** (or update-by-name) a single entry inside the existing `models:` array, leaving siblings alone. It must not touch `name`, `version`, `context`, `rules`, `mcpServers`, or any other model entries the user has defined. For an existing `freeride` entry: replace by `name`. Do not remove other entries.

### Restart behavior
Continue hot-reloads `~/.continue/config.yaml` — the user does **not** need to restart VS Code or JetBrains; new model entries appear in the model dropdown within seconds of saving the file.

### Working minimum config (copy-paste)
```yaml
# ~/.continue/config.yaml — append this entry to models:[]
models:
  - name: FreeRide
    provider: openai          # for any OpenAI-compatible endpoint
    apiBase: http://localhost:11343/v1
    apiKey: any
    model: openrouter/free    # any model the gateway exposes via /v1/models
    roles:
      - chat
      - edit
      - autocomplete
```

The `provider: openai` value is correct for any OpenAI-compatible endpoint — Continue does **not** use `openai-compatible` as a separate provider name; it reuses `openai` and lets `apiBase` redirect.

### Quirks
- **Roles are per-model.** A model can declare `chat`, `edit`, `autocomplete`, `embed`, `rerank`. For chat-shaped FreeRide use, default to `[chat, edit]` and let the user opt into `autocomplete` (it tends to be latency-sensitive and might not benefit from gateway hops).
- **`name` is the user-facing label** in the model picker. Use a stable value like `FreeRide` so re-binds idempotently update the same entry.
- **`capabilities`** is an optional sibling array (e.g., `tool_use`, `image_input`) — leave it unset; the gateway and chosen upstream model determine what works at request time.

---

## OpenCode (sst/opencode)

### Identity / docs
- Repo: https://github.com/sst/opencode
- Site: https://opencode.ai
- Docs root: https://opencode.ai/docs/
- Custom provider page: https://opencode.ai/docs/providers/#custom-provider
- Config schema: https://opencode.ai/config.json

Disambiguation note: there's also `anomalyco/opencode`. SST's is the active, well-known one (15kb+ of docs, AI-SDK-based provider model). The bind helper targets sst/opencode unless explicitly told otherwise.

### Config file
- Global: **`~/.config/opencode/opencode.json`**
- Project-local: `opencode.json` at repo root (overrides global per-project)
- Format: **JSON or JSONC** (JSON with comments). (https://opencode.ai/docs/config)
- Top-level keys observed: `$schema`, `provider`, `model`, `small_model`, plus general settings.

### Base URL knob
- Path: **`provider.<provider_id>.options.baseURL`**
- The `provider_id` is a free-form string the user picks (we'll use `freeride`).
- (https://opencode.ai/docs/providers/#custom-provider)

### API key knob
- Path: `provider.<provider_id>.options.apiKey` — supports env-var substitution: `"{env:OPENAI_API_KEY}"`.
- For the gateway's `any` key, hardcode `"any"` — substitution is optional.

### Keys to preserve
- Top-level `model`, `small_model`, plus any other `provider.*` blocks the user has configured.
- Inside our own `provider.freeride` block: nothing — the helper owns this block.

### Restart behavior
OpenCode is a TUI; config is re-read on next prompt or via `/reload`. Hot-reload, no full restart needed.

### Working minimum config (copy-paste)
```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "freeride": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "FreeRide Gateway",
      "options": {
        "baseURL": "http://localhost:11343/v1",
        "apiKey": "any"
      },
      "models": {
        "openrouter/free": { "name": "Free smart-router" }
      }
    }
  }
}
```

### Quirks
- **`npm` field is critical.** Use `@ai-sdk/openai-compatible` for any provider exposing `/v1/chat/completions`. Use `@ai-sdk/openai` only if the upstream uses `/v1/responses` (the new OpenAI Responses API). FreeRide's gateway is chat-completions — so `@ai-sdk/openai-compatible`.
- **Models must be enumerated** in `provider.<id>.models`. OpenCode does not auto-discover from `/v1/models` (per the docs example); the bind helper should pre-populate at least one model entry, ideally pulling from the gateway's `/v1/models` at bind time.
- **`name` is for display only.** Choose something the user will recognize in the `/models` picker.

---

## Cross-cutting notes

### What `freeride bind` is and isn't
Per `PLAN_GATEWAY.md` §6: bind helpers are **two-line file-touchers**, not a generalized Consumer plugin layer. Each helper:
1. Reads the existing config (handle missing-file as empty defaults).
2. Sets *exactly* the URL + key + a name/model entry — nothing else.
3. Writes back atomically (`core/state.atomic_write`).
4. Prints a one-line confirmation and any restart hint.

Keys to preserve are agent-specific but the rule is the same for all three: read → minimal patch → write. Conformance test: same fixture in, run bind, diff — only the documented keys changed.

### Tested-vs-inferred status
- **Aider**: keys verified directly against the options reference and aider_conf.html (https://aider.chat/docs/config/options.html, /aider_conf.html). YAML key `openai-api-base` confirmed.
- **Continue**: `provider`, `apiBase`, `model`, `roles` confirmed via https://continue.dev/reference. `apiKey` not shown in the canonical example but ubiquitous in provider sections; should be verified live during Phase 4 implementation.
- **OpenCode**: full block verified via https://opencode.ai/docs/providers/#custom-provider including `npm` package name, `options.baseURL`, schema URL.

### Open questions to settle during Phase 4 implementation
1. **Continue config-file location precedence.** Some installs use `~/.continue/config.json` (legacy). The bind helper should detect whichever file exists, prefer YAML, and fall back to JSON only if the YAML doesn't exist.
2. **Aider `.aider.conf.yml` location precedence.** Aider's three-place search (git root → cwd → home) means the bind helper must decide which one to write. Default to `~/.aider.conf.yml` (home), but document a `--scope=cwd|git|home` flag for users who keep per-project Aider config.
3. **OpenCode model list bootstrapping.** At bind time, fetch the gateway's `/v1/models` and pre-populate `provider.freeride.models{}` so the user has something to pick. Refresh on `freeride bind opencode --refresh`.
4. **Hermes** — separate research note (docs/hermes.md). Not covered here.

### Not in scope (per `PLAN_GATEWAY.md` §15)
- llama.cpp, LM Studio — local inference, no gateway need.
- IDE-direct extensions (Cursor, Zed AI) — covered if/when they expose a `OPENAI_API_BASE`-equivalent knob; if not, they don't ship a bind helper.
