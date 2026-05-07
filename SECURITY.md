# Security policy

## Supported versions

Only the latest minor release receives security fixes. FreeRide is pre-1.0 and ships frequently — pin a version and upgrade often.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Email `shaivpidadi@gmail.com` with:

- The vulnerability and how to reproduce it
- The version of `freeride-gateway` you're running (`freeride --version`)
- Any logs or PoC code

You'll get an acknowledgement within 72 hours. If the issue is confirmed, we'll coordinate a fix and a disclosure timeline before publishing.

## What's in scope

- The `freeride-gateway` PyPI package and the `freeride` CLI
- The Cloudflare Worker at `services/telemetry/` (running at `telemetry.free-ride.xyz` and `free-ride.xyz`)
- The install script at `https://api.free-ride.xyz/install.sh`

## What's out of scope

- Bugs in upstream provider APIs (OpenRouter, Groq, NVIDIA NIM, Cloudflare Workers AI, HuggingFace) — report those to the provider.
- Issues in agent clients that bind to FreeRide (Aider, Continue, OpenClaw, Hermes) — report those to the agent project.
- Local exploits that require write access to the user's home directory (FreeRide reads its config from `~/.freeride/`).

## Telemetry data and privacy

FreeRide ships with default-on aggregate telemetry. The exact payload is documented in `README.md` and shown to the user via a one-time disclosure banner before it's ever sent. **Prompts, completions, model IDs, API keys, hostnames, and IPs are never sent.** The Cloudflare Worker that receives the beacon does not log `cf-connecting-ip`.

If you find a way to make FreeRide leak content it shouldn't, that's a security issue — please report it as above.
