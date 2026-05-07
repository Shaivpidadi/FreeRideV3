# Go-live checklist — FreeRide v0.3.0

> Status: **0.3.0a2 is live on PyPI** as of 2026-05-07.
> https://pypi.org/project/freeride-gateway/0.3.0a2/
>
> Telemetry backend is deployed on Cloudflare Workers + D1. DNS for
> `telemetry.free-ride.xyz` is propagating (started 16:15 UTC; ETA
> ~17:15 UTC). Until then, beacons fail silently — no user impact.

---

## Done ✅

### Client (PyPI 0.3.0a2)

- PyPI release: `freeride-gateway==0.3.0a2`
- Tag `v0.3.0a2` pushed
- 175 unit tests + 6 e2e tests, all green
- E2e covers real Aider, Hermes, openai-python SDK, OpenClaw schema
- LICENSE file at repo root (MIT, attributed)
- Default-on telemetry with first-run disclosure banner — banner shows
  exact payload, opt-out command, audit command; persists
  `telemetry_disclosure_shown` so it never re-prints
- README on PyPI page leads the telemetry section with "ENABLED BY
  DEFAULT, opt out anytime" — no burying

### Backend (services/telemetry/)

- Cloudflare Worker deployed: `freeride-telemetry`
- D1 database created: `freeride-telemetry` (id in wrangler.toml)
- Schema applied: `beacons` table + indexes on `received_at` and `installation_id`
- Worker URL (always-on): `https://freeride-telemetry.shaivpidadi.workers.dev`
- Custom domain route bound in wrangler: `telemetry.free-ride.xyz`
- Three routes verified end-to-end:
  - `GET /health` → `{"ok":true}`
  - `POST /v1/beacon` validates + stores
  - `GET /v1/stats` returns aggregate counters
- Worker explicitly does NOT log or store IPs / hostnames
- Free-tier quota: 100k req/day, 5GB D1 — ~1000× headroom for v0.3.x

### Audit / housekeeping

- No real API keys leak into the sdist (verified)
- Tarball includes `LICENSE` + README; PyPI page renders correctly
- All major imports resolve cleanly from PyPI install

---

## In flight ⏳

### DNS propagation for free-ride.xyz

GoDaddy → Cloudflare nameservers, started 2026-05-07 ~16:15 UTC.
Propagation typically completes within an hour; once it does:
- `curl https://telemetry.free-ride.xyz/health` returns `{"ok":true}`
- All running 0.3.0a2 installs (which have telemetry on by default)
  start landing beacons

No action needed during the wait.

---

## P0 — must do before bumping to 0.3.0 final

### Verify production beacon path end-to-end

After DNS propagates:
```bash
curl https://telemetry.free-ride.xyz/health   # expect {"ok":true}
freeride telemetry off && freeride telemetry on   # reset disclosure flag
freeride serve                                # any short-lived run
# wait ≤1h or trigger a beacon manually
wrangler d1 execute freeride-telemetry --remote \
  --command "SELECT * FROM beacons ORDER BY received_at DESC LIMIT 5"
# expect at least 1 row from this install
```

### Real-user smoke test on a non-developer machine

Pick **one** other person — not the maintainer, not the assistant:
```
pip install --pre freeride-gateway
freeride serve
freeride bind aider     # or whichever agent they use
# run a real prompt
```

Goal: catch any "fresh shell / Linux / Python 3.10" install corner you
don't hit on your dev box. Anything that breaks gets fixed before the
0.3.0 final bump.

### Tighten the PyPI token scope

The token used for first uploads is account-wide. Now that the project
exists on PyPI:
1. https://pypi.org/manage/account/token/ → revoke the wide-scope token
2. Generate a new token scoped to `freeride-gateway` only
3. Or set up trusted publishing (GitHub Actions OIDC) — no token at all

Even better: trusted publishing setup at
https://pypi.org/manage/project/freeride-gateway/settings/publishing/.
Five-minute job; once done, no PyPI tokens ever again.

---

## P1 — should do before public announcement

### Bump to 0.3.0 (final, not alpha)

Once P0 is clean:
- `freeride/__init__.py`: `__version__ = "0.3.0"`
- `pyproject.toml` classifier: `Development Status :: 4 - Beta`
- Build, push to PyPI as `freeride-gateway==0.3.0`. Now
  `pip install freeride-gateway` (no `--pre`) picks it up.
- Tag `v0.3.0`.

### CI on push

`.github/workflows/test.yml` running `pytest` on every push (free for
public repos). Optional `pytest -m e2e` job that needs the OpenRouter
key as a GH Actions secret.

### CHANGELOG.md

`CHANGELOG.md` at repo root; Keep-A-Changelog format. List the major
0.3.0 capabilities (gateway, binders, providers, telemetry).

### CONTRIBUTING.md

For people who want to add a Provider plugin — point at
`freeride.core.provider.Provider`, the conformance suite, and
`knowledge/providers/SURVEY.md`.

### Public stats page

`/v1/stats` already returns the aggregate JSON. A simple HTML page on
free-ride.xyz that reads it and shows "X tokens served by N installs"
makes the project's impact visible. ~30 lines of HTML on Cloudflare
Pages, free.

---

## P2 — nice to have, can land post-announcement

- OpenCode binder (Phase 4.9 extended target)
- Anthropic Messages API surface (Phase 6.1 — when ≥2 real clients ask)
- Embeddings endpoint (Phase 6.2 — same)
- Groq / Cloudflare WAI / HuggingFace providers (Provider Protocol
  already absorbs them per `knowledge/providers/SURVEY.md`)
- Demo gif / 60-second screencast
- Self-hosted deployment guide for users who want the gateway on a
  shared server (not localhost)

---

## Announcement plan (post P0)

After 0.3.0 final is up and the smoke test passes, in priority order:

1. **r/LocalLLaMA** — primary audience, ~350k subs.
2. **Hacker News** — title focus on the "free across providers,
   transparent failover" angle.
3. **X/Twitter** — short thread with the architecture diagram + demo
   gif.
4. **OpenRouter Discord** — they have a "show and tell" channel.
5. **Aider Discord** — Aider users will care because we make Aider
   work on free models cleanly.
6. **NousResearch / Hermes** — they were the trigger for v3 issue #11.

Soft launches at lunch hours (US Pacific 11am, EU 14:00 UTC) tend to
get more engagement than late-night posts.

---

## What this checklist does NOT promise

- Production-grade SLAs. This is a **local, BYO-keys** tool.
- Security review. The gateway sits in your request path; if you want
  a third-party audit before depending on it for sensitive work, get
  one.
- Backward compatibility for the Provider Protocol. `api_version = 1`
  is frozen; future breaking changes bump it.
- Bug-free. We're 0.3.0a2. Things will break. PyPI is the start, not
  the end.
