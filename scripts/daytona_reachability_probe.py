"""Daytona reachability probe — confirms which provider endpoints + FreeRide
infrastructure URLs are reachable from a tier-3 sandbox.

Run:
    DAYTONA_API_KEY=... python scripts/daytona_reachability_probe.py

Spins up an ephemeral sandbox, curls each endpoint, prints a status table,
deletes the sandbox. Total wall time: ~60-90 seconds. Sandbox cost: a few
cents at most.

Hard data over assumptions. Earlier sessions showed tier 1/2 sandboxes
blocked Cloudflare-fronted endpoints (api.cerebras.ai, api.free-ride.xyz,
integrate.api.nvidia.com). Tier 3 should lift that restriction per the
Daytona docs. This script verifies that empirically before we design any
CI infrastructure that assumes those endpoints are reachable.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass


# Endpoints to probe. Mix of:
#  - free-tier provider APIs FreeRide routes through
#  - Anthropic (passthrough target)
#  - FreeRide's own Cloudflare-Pages-hosted infra
#  - PyPI / npm / GitHub (sanity — should always be reachable)
ENDPOINTS = [
    # FreeRide infra (Cloudflare Pages — blocked on tier 1/2)
    ("api.free-ride.xyz",         "https://api.free-ride.xyz/health"),
    # Anthropic (passthrough target — always reachable)
    ("api.anthropic.com",         "https://api.anthropic.com/"),
    # Free providers FreeRide aggregates
    ("api.groq.com",              "https://api.groq.com/"),
    ("openrouter.ai",             "https://openrouter.ai/"),
    ("api-inference.huggingface.co", "https://api-inference.huggingface.co/"),
    ("api.cerebras.ai",           "https://api.cerebras.ai/"),
    ("integrate.api.nvidia.com",  "https://integrate.api.nvidia.com/"),
    # Sanity baseline
    ("pypi.org",                  "https://pypi.org/"),
    ("github.com",                "https://github.com/"),
    ("registry.npmjs.org",        "https://registry.npmjs.org/"),
]


@dataclass
class ProbeResult:
    host: str
    http_status: str  # "200", "404", "FAIL", etc.
    elapsed_ms: int
    note: str


def _build_probe_script() -> str:
    """Build a bash one-liner that probes each endpoint and emits one
    TSV line per result. We use TSV so parsing it back on the host side
    is trivial (no JSON deps required inside the sandbox).
    """
    lines = []
    for host, url in ENDPOINTS:
        # `curl -o /dev/null -w "%{http_code}\t%{time_total}\n" --max-time 8`
        # exits 0 on connect success even for 4xx/5xx HTTP responses. exit
        # code 6/7/28/etc. means connect/DNS/timeout — we sentinel those
        # as HTTP=000 to keep parsing uniform.
        lines.append(
            f'printf "{host}\\t"; '
            f'curl -sS -o /dev/null --max-time 8 '
            f'-w "%{{http_code}}\\t%{{time_total}}\\n" '
            f'"{url}" 2>/dev/null || printf "000\\t-1\\n"'
        )
    return "\n".join(lines)


def _parse_results(stdout: str) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    for line in stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        host, http_code, t = parts[0], parts[1], parts[2]
        try:
            elapsed_ms = int(float(t) * 1000) if t and t != "-1" else -1
        except ValueError:
            elapsed_ms = -1
        if http_code == "000":
            note = "connect failed (DNS/firewall/timeout)"
        elif http_code.startswith(("2", "3")):
            note = "ok"
        elif http_code.startswith("4"):
            note = "ok (4xx = TLS reached, endpoint just refused root path)"
        elif http_code.startswith("5"):
            note = "server error (but reachable)"
        else:
            note = "unknown"
        out.append(ProbeResult(host, http_code, elapsed_ms, note))
    return out


def _print_table(results: list[ProbeResult], tier_hint: str | None) -> None:
    header = f"  {'Host':<32} {'HTTP':<8} {'Time':<8} Note"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for r in results:
        time_s = f"{r.elapsed_ms}ms" if r.elapsed_ms >= 0 else "—"
        print(f"  {r.host:<32} {r.http_status:<8} {time_s:<8} {r.note}")
    print()

    n_ok = sum(1 for r in results if r.http_status != "000")
    n_blocked = sum(1 for r in results if r.http_status == "000")
    print(f"  reachable: {n_ok}/{len(results)}")
    if n_blocked:
        blocked = [r.host for r in results if r.http_status == "000"]
        print(f"  blocked:   {', '.join(blocked)}")
    if tier_hint:
        print(f"  tier hint: {tier_hint}")


def main() -> int:
    if not os.environ.get("DAYTONA_API_KEY"):
        print(
            "DAYTONA_API_KEY not set.\n"
            "Get one at https://app.daytona.io/dashboard/keys, then:\n"
            "  export DAYTONA_API_KEY=dtn_...\n"
            "  python scripts/daytona_reachability_probe.py",
            file=sys.stderr,
        )
        return 2

    try:
        from daytona import Daytona, CreateSandboxFromImageParams, Image
    except ImportError:
        print("Daytona SDK not installed. Run: pip install daytona", file=sys.stderr)
        return 2

    print("Daytona reachability probe")
    print(f"  endpoints: {len(ENDPOINTS)}")
    print()

    daytona = Daytona()

    print("→ creating ephemeral sandbox...")
    t0 = time.perf_counter()
    sandbox = daytona.create(CreateSandboxFromImageParams(
        image=Image.debian_slim("3.12"),
        name=f"freeride-reachability-{int(time.time())}",
    ))
    print(f"  sandbox up in {time.perf_counter() - t0:.1f}s (id={sandbox.id})")
    print()

    try:
        # Ensure curl is available (Debian slim has it, but be defensive)
        sandbox.process.exec("which curl || apt-get install -y curl >/dev/null 2>&1")

        print("→ probing endpoints (8s timeout each)...")
        t0 = time.perf_counter()
        response = sandbox.process.exec(_build_probe_script())
        elapsed = time.perf_counter() - t0
        print(f"  probes complete in {elapsed:.1f}s")
        print()

        results = _parse_results(response.result)
        # Heuristic tier hint based on which endpoints reached
        cf_endpoints = {"api.free-ride.xyz", "api.cerebras.ai", "integrate.api.nvidia.com"}
        cf_reached = sum(
            1 for r in results if r.host in cf_endpoints and r.http_status != "000"
        )
        if cf_reached == len(cf_endpoints):
            tier_hint = "tier 3+ (Cloudflare-fronted endpoints reach OK)"
        elif cf_reached > 0:
            tier_hint = "partial — some Cloudflare endpoints reach, some blocked"
        else:
            tier_hint = "tier 1/2 (Cloudflare-fronted endpoints all blocked)"
        _print_table(results, tier_hint)

    finally:
        print()
        print(f"→ deleting sandbox {sandbox.id}...")
        sandbox.delete()
        print("  done")

    return 0


if __name__ == "__main__":
    sys.exit(main())
