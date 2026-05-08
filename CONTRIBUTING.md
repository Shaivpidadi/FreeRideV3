# Contributing to FreeRide

The most useful contributions right now are **new Provider plugins** (Groq,
Cloudflare Workers AI, HuggingFace Inference, Together, DeepInfra, …) and
**new agent binders** (OpenCode, Cursor, etc.). Both are bounded — adding a
provider is ~200 lines following the NIM/OpenRouter template; a binder is
~50 lines plus tests.

## Local setup

```bash
git clone https://github.com/Shaivpidadi/FreeRideV3
cd FreeRideV3
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                   # hermetic unit suite, ~0.4s
```

For the e2e suite (real Aider, real Hermes, real OpenRouter):

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
pytest -m e2e
```

## Adding a Provider plugin

The contract lives in `freeride/core/provider.py` — `Provider` Protocol with
`api_version = 1`. Any class that implements all the methods works; you don't
need to import or subclass anything.

### Steps

1. **Create `freeride/providers/<your_provider>.py`** implementing the
   Protocol. The shape:
   ```python
   class YourProvider:
       name = "your_provider"
       api_version = PROVIDER_API_VERSION

       def list_free_models(self, key) -> list[Model]: ...
       def probe(self, model_id, key) -> ProbeResult: ...
       async def forward_chat(self, request, model_id, key) -> ChatResponse: ...
       async def forward_chat_stream(self, request, model_id, key) -> AsyncIterator[ChatStreamEvent]: ...
       def classify_error(self, response_or_exc) -> ErrorKind: ...
       def retry_after_hint(self, response) -> int | None: ...
       def auth_header(self, key) -> dict[str, str]: ...
       def attribution_headers(self) -> dict[str, str]: ...
   ```

   Use `freeride/providers/openrouter.py` as the canonical template;
   `freeride/providers/nvidia_nim.py` if your provider needs metadata
   sidecars or non-standard error shapes.

2. **If your provider's catalog endpoint doesn't expose context length /
   capability fields**, add a `freeride/providers/<your_provider>_model_metadata.py`
   sidecar (see `nim_model_metadata.py`).

3. **Add to the conformance suite registry** in
   `tests/conformance/test_provider_conformance.py`:
   ```python
   from freeride.providers.your_provider import YourProvider
   CONFORMANT_PROVIDERS: list[type] = [
       NoopProvider,
       OpenRouterProvider,
       NVIDIANIMProvider,
       YourProvider,         # ← add here
   ]
   ```

   The 11 invariants run automatically — they cover Protocol shape,
   `api_version`, type-correctness of returned values.

4. **Add to the auto-load list** in `freeride/cli/cmd_serve.py` so the
   gateway picks it up when `<YOUR_PROVIDER>_API_KEY` is set:
   ```python
   if os.environ.get("YOUR_PROVIDER_API_KEY"):
       providers.append(YourProvider())
   ```

5. **Write tests** at `tests/providers/test_<your_provider>.py` — use
   `tests/providers/test_openrouter.py` as a template. Mock httpx with
   `pytest-httpx`; keep the entire suite hermetic. If you want a live-API
   integration test, put it in `tests/e2e/test_<your_provider>.py` with
   `@pytest.mark.e2e`.

### Provider Protocol gotchas

- `api_version` must equal `PROVIDER_API_VERSION` (currently `1`).
  Future breaking changes bump the constant; old plugins are skipped at
  load time, not silently broken.
- `classify_error` should accept either a response object OR an exception
  — handle both cases.
- `attribution_headers()` returns `{}` if your provider has no
  app-attribution mechanism. Don't invent fake headers.
- The same plugin's methods are called from both sync (CLI) and async
  (gateway request handler) contexts. Keep methods that don't need to be
  async sync; the async ones (`forward_chat`, `forward_chat_stream`) are
  the ones that hit the request hot-path.
- Free-detection: see `docs/providers/SURVEY.md` for the three patterns
  in the wild (per-model flag, global free-credit pool, per-model RPM/TPM
  caps).

### Documentation

Add a per-provider reference at `docs/providers/<your_provider>.md`
covering: auth, catalog endpoint, free-tier semantics, probe convention,
error classification, streaming, capabilities, attribution, and any
OpenAI-compat deltas. The NIM and OpenRouter docs in there are good
templates.

## Shipping a provider as a separate pip package

In-tree providers (the six that live in `freeride/providers/`) cover the
big free tiers. If you want to ship a provider for something else without
contributing to FreeRide directly, package it as its own pip distribution
and register the class via Python entry points — FreeRide auto-discovers
them at startup.

### Minimal third-party plugin

```python
# my_freeride_plugin/awesome.py
from freeride.core.provider import PROVIDER_API_VERSION

class AwesomeProvider:
    name = "awesome"
    api_version = PROVIDER_API_VERSION
    embeddings_supported = False  # or True if you implement forward_embeddings

    def __init__(self):
        # Raise ValueError when required env vars are missing — FreeRide's
        # registry treats this as "I'm not configured, skip me" and logs at
        # INFO. The gateway keeps starting with the remaining providers.
        import os
        api_key = os.environ.get("AWESOME_API_KEY")
        if not api_key:
            raise ValueError("AwesomeProvider requires AWESOME_API_KEY")

    # ... implement the rest of the Provider Protocol ...
```

```toml
# my_freeride_plugin/pyproject.toml
[project]
name = "freeride-awesome-provider"
version = "0.1.0"
dependencies = ["freeride-gateway>=0.4.0a1", "httpx>=0.27,<1"]

[project.entry-points."freeride.providers"]
awesome = "my_freeride_plugin.awesome:AwesomeProvider"
```

After `pip install freeride-awesome-provider`, the plugin is discovered
automatically by `freeride serve`. Wire it the same way as any built-in
provider — set `AWESOME_API_KEY` in env and run.

### Trust model

Plugins run in-process — no sandbox. Users opt in by `pip install`-ing
your package, which is the same trust model as any Python dependency.
Document any side effects, network calls, or filesystem touches in your
plugin's README so users can audit before installing.

### Compatibility

Plugins MUST declare `api_version = PROVIDER_API_VERSION` from
`freeride.core.provider`. The registry skips plugins on a different
version with a warning so users can see they need to upgrade. Bumping
the Provider Protocol is rare (it's been at 1 since 0.3.0); we'll
announce in the CHANGELOG when it changes.

## Adding an agent binder

Binders live in `freeride/binders/<agent>.py`. Each binder exposes one
function:

```python
def bind(gateway_url: str, *, api_key: str = "any", config_path: Path | None = None) -> str:
    ...
    return "human-readable status message printed by the CLI"
```

### Rules

- **Atomic-write the config file** via `freeride.core.state.atomic_write`
  or `write_json_atomic`. Never use plain `open(path, "w")`.
- **Preserve all unrelated keys.** A user with an existing config has every
  right to expect their unrelated settings (like Continue's other model
  entries) to round-trip exactly.
- **Idempotent re-runs.** Running `freeride bind <agent>` twice should not
  duplicate entries.
- **Don't store API keys you didn't introduce.** If the agent's config
  format encrypts secrets in a separate file (OpenClaw's auth-profiles.json),
  let the agent's own CLI populate it — don't write to that file directly
  unless the format is documented and stable.
- **Handle the "agent not installed" case gracefully.** The binder writes
  config; it should NOT try to install or upgrade the agent itself.

### Hooking into the dispatcher

Add a new branch to `freeride/cli/cmd_bind.py`:

```python
if agent == "your_agent":
    from freeride.binders import your_agent
    print(your_agent.bind(gateway_url))
    return 0
```

And add `"your_agent"` to the `choices=` list on the CLI parser in
`freeride/cli/main.py` (the `bind` subparser).

### Tests

Add `tests/test_binders.py::TestYourAgentBinder` covering:
- creates a new config file when none exists
- preserves comments/structure of an existing config
- preserves unrelated keys
- idempotent re-runs

If the agent has a CLI mode that's pytest-friendly (Aider's `--message`,
Hermes's `-z`), add an e2e test under `tests/e2e/test_<your_agent>.py`
that spins up the gateway and runs a real prompt.

## Coding standards

- **Type hints** required on public functions / class members.
- **Docstrings** at the module and public-class level. Don't add
  one-line docstrings that just restate the function name; either
  explain *why* / *what's non-obvious*, or skip.
- **No comments that describe what well-named code already says.**
  If you find yourself writing `# Loop over the list`, delete it.
- **Format with `ruff format`** (line length 100).
- Follow the existing patterns in `freeride/providers/openrouter.py` and
  `freeride/binders/openclaw.py` if in doubt.

## Commits

- Imperative subject (`add groq provider`, not `added groq provider`).
- Lowercase, no period.
- Body wraps at ~72 chars; explain *why* not *what*.
- Don't add `Co-Authored-By: Claude / Cursor / etc.` — house style is
  human-attributed commits.
- One logical change per commit. CI runs on every push so split-up
  history is cheap.

## Roadmap visibility

- [`docs/providers/SURVEY.md`](docs/providers/SURVEY.md) — Provider Protocol fit per provider
- [`docs/agent-binders.md`](docs/agent-binders.md) — agent bind reference
- [GitHub Issues](https://github.com/Shaivpidadi/FreeRideV3/issues) — current backlog and roadmap

For new product directions, open a GitHub issue first to discuss.

## License

By contributing, you agree your contributions will be licensed under the
project's MIT License (see [LICENSE](./LICENSE)).
