# Go-live checklist — FreeRide v0.3.0

> Status: **0.3.0a1 is live on PyPI** as of 2026-05-07.
> https://pypi.org/project/freeride-gateway/0.3.0a1/
>
> This doc tracks what's needed to promote alpha → public v0.3.0 and announce it.

---

## Already in place ✅

- PyPI release `freeride-gateway 0.3.0a1` (alpha; opt-in via `--pre`)
- Real-PyPI install path verified end-to-end (fresh venv `pip install --pre freeride-gateway`)
- 174 unit tests + 6 e2e tests, all green
- E2e covers real Aider, Hermes, openai-python SDK, OpenClaw schema
- License declared MIT in `pyproject.toml`, propagated to PyPI metadata
- README on PyPI page renders correctly with install + quick start + telemetry transparency
- GitHub repo public, tag `v0.3.0a1` pushed
- NIM provider works (locally; Daytona Tier 1/2 blocks NIM, documented)

## Tarball / metadata audit (passed)

- No real API keys leak into the sdist (checked: only redacted docstring examples)
- Distribution name `freeride-gateway` distinct from existing `freeride` (typo-squatting policy passed)
- CLI binary stays `freeride` — independent of distribution name
- All major imports resolve cleanly from a PyPI install: `freeride.providers.{openrouter,nvidia_nim}`, `freeride.binders.{openclaw,aider,continue_,hermes}`, `freeride.server.app`

---

## P0 — must do before public announcement

### Telemetry endpoint reality check

The beacon URL hard-coded in `freeride/core/telemetry.py` is `https://telemetry.freeride.dev/v1/beacon` — that domain **does not exist**. If a user opts in via `freeride telemetry on`, every beacon POST silently fails. Not user-visible breakage (the failure is silent by design), but the `freeride telemetry` UI tells them it's "POSTing hourly to the documented endpoint" — which is misleading.

Three options:
- **(a) Stand up the endpoint.** A 5-line FastAPI app on Vercel / Cloudflare Workers / your own VM that accepts POSTs and bins counters into a SQLite or DuckDB. Persistently free under most plans. Then you actually get the visibility data the spec promised.
- **(b) Mark telemetry as "preview / no backend yet"** in the CLI output. `freeride telemetry on` prints a warning that beacons are dropped on the floor. Honest; less work.
- **(c) Remove the telemetry feature entirely from this release.** Re-add when there's a real backend. Smallest blast radius if you're not sure.

Recommend (a) if you have an hour to spare. Otherwise (b) is the defensible path. (c) is overkill.

### Add LICENSE file at repo root

`pyproject.toml`'s `license = "MIT"` is recognized but a top-level `LICENSE` file is convention and shows up in the GitHub UI. Standard MIT text, attribute to "Shaishav Pidadi 2026".

### Real-user smoke test on a non-developer machine

Pick **one** new person — not you, not me. Have them:
```
pip install --pre freeride-gateway
freeride serve
freeride bind aider     # or whichever agent they use
# run a real prompt
```

Goal: catch any "Python 3.10 / Linux / fresh shell" install corner you don't hit on your dev box. Anything that breaks here gets fixed before announcement.

### Set the PyPI token scope correctly

The token used for the first upload is account-wide. Now that the project exists on PyPI:
1. https://pypi.org/manage/account/token/ → revoke the wide-scope token
2. Generate a new token scoped only to `freeride-gateway`
3. Save the new token wherever you keep secrets — you'll need it for v0.3.0 (which I'll cut once we're ready)

Even better: **trusted publishing via GitHub Actions OIDC** — no token at all. Setup at https://pypi.org/manage/project/freeride-gateway/settings/publishing/. Wire up a GitHub Actions workflow that publishes on tag push. Five-minute job; once done, no PyPI tokens ever again.

---

## P1 — should do before announcement

### Bump to 0.3.0 (final, not alpha)

Once P0 above is clean, bump:
- `freeride/__init__.py`: `__version__ = "0.3.0"`
- `pyproject.toml` classifier: `Development Status :: 4 - Beta` (or `5 - Production/Stable` if you're confident)

Rebuild, push to PyPI as `freeride-gateway==0.3.0`. Now `pip install freeride-gateway` (without `--pre`) picks it up.

Tag `v0.3.0` on GitHub.

### CI on push

`.github/workflows/test.yml` that runs `pytest` (regular suite, no e2e) on every push. Free for public repos. Catches regressions before they reach `main`. Optional `pytest -m e2e` job that needs the OpenRouter key as a GitHub Actions secret.

### CHANGELOG.md

A `CHANGELOG.md` at repo root with the v0.3.0 entry. Keep-A-Changelog format. List the major capabilities (gateway, binders, providers, telemetry).

### CONTRIBUTING.md

For people who want to add a Provider plugin. Point at `freeride.core.provider.Provider`, the conformance suite, and `knowledge/providers/SURVEY.md`.

### Project description on PyPI

Currently shows the raw README. Verify on https://pypi.org/project/freeride-gateway/ that:
- The diagram renders (it's plain text, should be fine)
- All internal links resolve to the GitHub repo (Project-URL "Repository" is set)

---

## P2 — nice to have, can land post-announcement

### OpenCode binder (Phase 4.9 extended target)

`freeride bind opencode` writes the right block to `~/.config/opencode/opencode.json`. Mostly mechanical — schema research already done in `knowledge/CONSUMERS.md`.

### Anthropic Messages API surface (Phase 6.1)

For Claude-shaped clients. Decision criterion from PLAN_GATEWAY.md §13: ≥2 real Anthropic-API-shaped clients ask. If one user posts that they want it on day 1, do it; otherwise hold.

### Embeddings endpoint (Phase 6.2)

`POST /v1/embeddings`. Same Provider Protocol shape. Land if there's demand.

### More providers

`SURVEY.md` validated Groq, Cloudflare WAI, HuggingFace Inference all fit. Each is ~200 lines following the NIM template. Ship one per week as users ask.

### Self-hosted deployment guide

For users who want to run the gateway on a server (not just localhost). Docker image + a one-page README section.

### Demo recording / screencast

A 60-second screen recording of `pip install` → `freeride serve` → real Aider session. Lifts conversion noticeably for projects like this.

---

## Announcement plan (P2)

Once 0.3.0 is live and the smoke test passes, post in (priority order):

1. **r/LocalLLaMA** — primary audience. 350k+ subscribers actively interested in free LLM stacks.
2. **Hacker News** — title focus on the "free across providers, transparent failover" angle. Keep the post text short.
3. **X/Twitter** — short thread with the architecture diagram and demo gif.
4. **OpenRouter Discord** — they have a "show and tell" channel; their team usually engages with downstream tooling.
5. **Aider Discord** — Aider's community will care because we make Aider work on free models cleanly.
6. **Hermes / NousResearch** — they were the trigger for v3 issue #11; they'll appreciate that the integration landed.

Soft launches at lunch hours of relevant timezones (US Pacific 11am, EU 14:00 UTC) tend to get more eyes than late-night posts.

---

## What this checklist DOESN'T promise

- Production-grade SLAs. This is a **local, BYO-keys** tool; "uptime" is whatever your laptop's uptime is.
- Security review. The gateway sits in your request path; if you want a third-party audit before depending on it for sensitive work, get one.
- Backward compatibility for the Provider Protocol. `api_version = 1` is frozen; future breaking changes bump it. Plugins built today won't break silently.
- Bug-free. We're 0.3.0a1. Things will break. The PyPI release is the start, not the end.
