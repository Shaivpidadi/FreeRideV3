"""``freeride bench`` — per-provider latency comparison.

Hits a running gateway with one tiny chat completion per registered
provider (using ``X-FreeRide-Force-Provider``), times each, prints a
sorted-by-p50 table with success rate, p50/p95 latency, and tokens-per-
second. Useful for:

- "Which provider is fastest right now?"
- "Is OpenRouter actually slower than Groq for this account?"
- Picking which to set as primary in a config file

Burns real tokens (it's a real chat completion). Default is 3 requests
per provider × however many are registered, with ``max_tokens=10`` per
call — typically <200 tokens total. Override with ``--n``.

Requires ``freeride serve`` to be running already; bench is read-only
on the gateway.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx


_BENCH_PROMPT_DEFAULT = "Reply with exactly one word: hi."
_BENCH_MAX_TOKENS = 10


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile. Returns 0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _list_providers(gateway_url: str) -> list[dict[str, Any]]:
    """Pull the registered-provider list from the gateway."""
    base = gateway_url.rstrip("/").removesuffix("/v1")
    resp = httpx.get(f"{base}/v1/_freeride/providers", timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("providers", [])


def _bench_one(
    *,
    gateway_url: str,
    provider: str,
    model: str,
    prompt: str,
    n: int,
) -> dict[str, Any]:
    """Run ``n`` chat completions against one provider, return aggregated stats."""
    base = gateway_url.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": "Bearer any",
        "Content-Type": "application/json",
        "X-FreeRide-Force-Provider": provider,
    }
    payload_template = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _BENCH_MAX_TOKENS,
        "stream": False,
    }

    durations_ms: list[float] = []
    completed_tokens: list[int] = []
    failures: list[str] = []
    statuses: list[int] = []

    with httpx.Client(timeout=30.0) as client:
        for i in range(n):
            t0 = time.perf_counter()
            try:
                r = client.post(url, headers=headers, json=payload_template)
            except httpx.HTTPError as e:
                failures.append(f"network: {e!s}")
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000
            statuses.append(r.status_code)
            if r.status_code != 200:
                # Capture a brief error reason from the body if available.
                try:
                    err = r.json().get("error", {}).get("type") or r.text[:80]
                except json.JSONDecodeError:
                    err = r.text[:80]
                failures.append(f"http {r.status_code}: {err}")
                continue
            durations_ms.append(elapsed_ms)
            try:
                usage = r.json().get("usage") or {}
                completed_tokens.append(int(usage.get("completion_tokens", 0)))
            except (json.JSONDecodeError, ValueError, TypeError):
                completed_tokens.append(0)

    ok = len(durations_ms)
    return {
        "provider": provider,
        "ok": ok,
        "n": n,
        "p50_ms": int(_percentile(durations_ms, 50)) if durations_ms else None,
        "p95_ms": int(_percentile(durations_ms, 95)) if durations_ms else None,
        "tok_per_s": (
            int(sum(completed_tokens) / sum(d / 1000 for d in durations_ms))
            if durations_ms and sum(completed_tokens) > 0
            else None
        ),
        "failures": failures,
        "statuses": statuses,
    }


def _format_table(rows: list[dict[str, Any]], *, no_color: bool) -> str:
    """Pretty-print a sorted-by-p50 table of bench rows."""
    sortable = [r for r in rows if r["p50_ms"] is not None]
    failed = [r for r in rows if r["p50_ms"] is None]
    sortable.sort(key=lambda r: r["p50_ms"])
    ordered = sortable + failed

    headers = ("provider", "ok", "p50", "p95", "tok/s")
    widths = [max(20, max(len(r["provider"]) for r in rows) + 2)]
    widths += [6, 8, 8, 8]

    lines: list[str] = []
    header_line = "".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "─" * sum(widths)
    lines.append(header_line)
    lines.append(sep)
    for r in ordered:
        ok_cell = f"{r['ok']}/{r['n']}"
        p50_cell = f"{r['p50_ms']}ms" if r["p50_ms"] is not None else "—"
        p95_cell = f"{r['p95_ms']}ms" if r["p95_ms"] is not None else "—"
        tok_cell = f"{r['tok_per_s']}" if r["tok_per_s"] is not None else "—"

        cells = [
            r["provider"].ljust(widths[0]),
            ok_cell.ljust(widths[1]),
            p50_cell.ljust(widths[2]),
            p95_cell.ljust(widths[3]),
            tok_cell.ljust(widths[4]),
        ]
        line = "".join(cells)
        if not no_color and r["p50_ms"] is None:
            line = f"\033[2m{line}\033[0m"  # dim failed rows
        lines.append(line)

    if sortable:
        fastest = sortable[0]
        winner_line = f"\nFastest: {fastest['provider']} ({fastest['p50_ms']}ms p50)"
        if not no_color:
            winner_line = f"\n\033[32mFastest:\033[0m {fastest['provider']} ({fastest['p50_ms']}ms p50)"
        lines.append(winner_line)
    if failed:
        why = ", ".join(f"{r['provider']} ({r['failures'][0] if r['failures'] else 'unknown'})" for r in failed)
        lines.append(f"Failed: {why}")
    return "\n".join(lines)


def cmd_bench(args) -> int:
    gateway_url = args.url.rstrip("/")
    if not gateway_url.endswith("/v1"):
        gateway_url = gateway_url + "/v1"

    no_color = bool(args.no_color) or not sys.stdout.isatty()

    # Pull the live provider list from the gateway. This also doubles as
    # a "is the gateway running?" probe.
    try:
        provider_objs = _list_providers(gateway_url)
    except httpx.ConnectError:
        print(
            f"error: gateway not reachable at {gateway_url}.\n"
            "       start it with `freeride serve` in another terminal first.",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as e:
        print(f"error: gateway returned {e!s}", file=sys.stderr)
        return 1

    if not provider_objs:
        print("error: gateway has no registered providers. Set at least one provider env var (e.g. OPENROUTER_API_KEY).", file=sys.stderr)
        return 1

    print(
        f"Benchmarking {len(provider_objs)} provider"
        f"{'s' if len(provider_objs) != 1 else ''}, "
        f"{args.n} request{'s' if args.n != 1 else ''} each via {gateway_url}…\n",
        file=sys.stderr,
    )

    rows: list[dict[str, Any]] = []
    for p in provider_objs:
        rows.append(
            _bench_one(
                gateway_url=gateway_url,
                provider=p["name"],
                model=args.model,
                prompt=args.prompt or _BENCH_PROMPT_DEFAULT,
                n=args.n,
            )
        )

    print(_format_table(rows, no_color=no_color))
    return 0
