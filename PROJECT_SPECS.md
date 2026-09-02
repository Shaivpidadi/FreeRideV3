# PROJECT_SPECS.md

## Project Overview
- **Project Name**: FreeRide
- **Version**: 0.4.0a22
- **Last Updated**: 2026-09-02
- **Primary Purpose**: Local OpenAI-compatible gateway that failovers across free-tier LLM providers and translates Claude Code, Codex, and Gemini CLI wire protocols.
- **Target Audience**: Developers running coding agents without paying vendor inference.

## Current Project Status
- **Development Stage**: Beta (0.4.0a* pre-release on PyPI)
- **Build Status**: GitHub Actions `test.yml` — pytest on Linux/macOS (3.10–3.13) and Windows (3.12); ruff; opt-in e2e
- **Test Coverage**: Hermetic unit suite under `tests/`; opt-in live e2e under `tests/e2e/`
- **Known Issues**: Mid-stream upstream death cannot failover (bytes already shipped). Health stats are in-memory only.
- **Next Milestone**: 0.4.0 stable

## Architecture Overview

### Tech Stack
- **Frontend**: N/A (CLI + local HTTP gateway)
- **Backend**: Python 3.10+, FastAPI, uvicorn, httpx, pydantic v2
- **Database**: None locally. Telemetry worker uses Neon Postgres.
- **Infrastructure**: PyPI (`freeride-gateway`), Cloudflare Worker at `services/telemetry/`
- **Development Tools**: pytest, ruff, hatchling

### System Architecture
- **Architecture Pattern**: Local reverse-proxy gateway with protocol translators
- **Key Components**:
  - `freeride/core/failover.py` — shared (provider, key) walk
  - `freeride/core/provider_env.py` — env-var registry
  - `freeride/core/cooldown.py` — hashed per-key cooldowns, TTL by error kind
  - Translators: `anthropic_translate.py`, `codex_translate.py`, `gemini_translate.py`
  - Providers under `freeride/providers/`
- **Data Flow**: Agent CLI → protocol route → translator → failover → upstream provider
- **External Dependencies**: OpenRouter, Groq, NVIDIA NIM, HuggingFace, Cerebras, Cloudflare Workers AI, Ollama; optional Anthropic passthrough

### Directory Structure
```
freeride/          package (CLI, core, providers, server, binders, v2compat)
tests/             unit, provider, e2e, conformance
docs/              agents, architecture, providers
services/telemetry Cloudflare Worker + Neon schema
```

## Core Features & Modules
- **Failover gateway**: health-ordered (provider, key) chain; structured 503s
- **Protocol shims**: `/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1beta/models/*:generateContent`, `/v1/embeddings`
- **CLI wrappers**: `freeride run claude|codex|gemini`
- **Binders**: Aider, Continue, Hermes, OpenClaw
- **Telemetry**: default-on aggregate beacon; opt out with `freeride telemetry off`

## Recent Changes Log
- **2026-09-02**: fx gateway dialect (`/v3/ai/language-model`, `/coding-agent/v1/models`) serving the ridex agent; universal provider/model fallback ladder; keepalive streaming pre-flight; honest mid-stream errors; `/health.keyed_providers`; SO_REUSEADDR port probe
- **2026-08-21**: Pin ruff to pre-0.16 E/F defaults; Windows tests set USERPROFILE for Path.home()
- **2026-08-21**: `freeride keys` loads `~/.freeride/.env` (same as `doctor` / `serve`) so CLI status matches configured keys
- **2026-08-21**: `NIM_API_KEY` accepted as alias for NVIDIA NIM (canonical remains `NVIDIA_API_KEY`)
