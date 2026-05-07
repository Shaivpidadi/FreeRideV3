# Daytona — environment notes for V3 build

> Reference doc captured during Phase 0 setup. Operational details for the
> $100-budgeted build; pricing snapshots are dated and can drift.

---

## Access verified (2026-05-07)

- **API base:** `https://app.daytona.io/api`
- **Auth:** `Authorization: Bearer <token>` (token stored locally, never committed)
- **Token scope:** sandbox + snapshot read/write only. `/api/users/me`, `/api/organizations`, billing endpoints all return 401 with this token. That's fine — narrow blast radius.
- **Probe results:**
  - `GET /api/sandbox` → 200, list of existing sandboxes
  - `GET /api/snapshots` → 200, includes default public `daytonaio/sandbox:0.6.0`
  - All other org/user/billing/usage paths → 401 or 404

## Existing sandboxes (4, all stopped)

Pre-existing in the org, all on `daytonaio/sandbox:0.6.0` (Python toolbox), 1 CPU / 1GB RAM / 3GB disk, last-active 2026-05-03 to 2026-05-05. Auto-stop 15 min, auto-archive 7 days, no auto-delete.

Decision: **don't reuse** these. Create a fresh dedicated sandbox `freeride-v3-build` so cost attribution is clean and lifecycle is self-contained.

## Pricing (from https://www.daytona.io/pricing, 2026-05-07)

- **CPU:** $0.0504/h per vCPU
- **RAM:** $0.0162/h per GiB
- **Disk:** $0.000108/h per GiB (first 5 GiB free)
- **Free credits:** $200 for new accounts; possible startup credits up to $50k

## Sandbox spec for V3 build

| Resource | Value | Cost/hr | Notes |
|---|---|---|---|
| CPU | 1 vCPU | $0.0504 | More than enough for FastAPI + pytest |
| RAM | 2 GiB | $0.0324 | 1 GiB risks OOM under pytest + httpx |
| Disk | 5 GiB | $0 | Within free tier; covers venv + project + caches |
| **Total** | | **~$0.083/hr** | |

**$100 runway:** ~1200 sandbox-hours of active runtime. With 15-min auto-stop and realistic dev cadence (build, test, push, idle), expected total burn for the full V3 build is **$3–10**. The $100 ceiling is generous.

## Cost tracking (manual — token can't query usage)

The provided token can't read `/api/billing` or `/api/usage`. I'll track manually:

1. Record sandbox `createdAt` from creation response.
2. After each sandbox restart, log timestamp.
3. Cost = `cpu_cost * runtime_hr + ram_cost * runtime_hr + max(0, disk_gb - 5) * disk_cost * runtime_hr`.
4. Report cumulative cost at every phase gate.
5. Halt at $80 cumulative, report, ask before continuing.

User can spot-check via the Daytona dashboard at any time.

## Operational rules

- **Auto-stop after 15 min idle.** Default behavior; keep it on. Do not disable.
- **Stop the sandbox** explicitly when paused for user input. Never leave it running idle for >2h with no commits.
- **Token storage.** Stored in shell env in the local terminal only (`DAYTONA_TOKEN`). Never written to repo files. `.env` files in the project should reference `${DAYTONA_TOKEN}` via env, not embed the value.
- **Sandbox env vars** for upstream API keys (OpenRouter, NIM) — set via Daytona env-var management, never committed.

## Useful API references

- Sandbox object shape: visible in `GET /api/sandbox` responses
- OpenAPI specs: `/docs/openapi.json` and `/docs/toolbox-openapi.json` (per docs)
- Toolbox proxy for in-sandbox command execution: `toolboxProxyUrl` field on each sandbox object (e.g., `https://proxy.app.daytona.io/toolbox`)

## What's left to confirm before Phase 1 starts

1. **Create sandbox.** Name `freeride-v3-build`, specs above. Awaiting user go-ahead.
2. **Set sandbox env vars** for `OPENROUTER_API_KEY` (and optionally a JSON-array of multiple keys for Phase 2 multi-key tests). User to provide.
3. **Set GitHub creds** in sandbox so commits push from inside it. Either an SSH key or a PAT.
4. **NVIDIA NIM key** — only needed at Phase 3, can defer.
