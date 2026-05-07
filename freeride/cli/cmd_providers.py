"""``freeride providers`` — show live provider health from a running gateway.

Pulls ``/v1/_freeride/providers`` from a ``freeride serve`` process and
pretty-prints what the gateway sees right now: registered providers,
embeddings support, recent attempt counts, success rate, p50 latency,
computed health score. Useful for the "is the gateway picking up my
providers? are they healthy?" introspection.

For per-request latency benchmarking with controlled load, see
``freeride bench``.
"""

from __future__ import annotations

import sys

import httpx


_HEALTH_MIN_N = 5  # mirrors freeride.core.health._min_n() default


def _format_table(provs: list[dict], *, no_color: bool) -> str:
    if not provs:
        return "(no providers registered — set at least one provider env var first)"

    headers = ("provider", "emb", "n", "ok%", "p50", "score", "")
    widths = [
        max(20, max(len(p["name"]) for p in provs) + 2),
        5, 6, 7, 9, 8, 9,
    ]

    lines: list[str] = []
    header_line = "".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "─" * sum(widths)
    lines.append(header_line)
    lines.append(sep)

    for p in provs:
        emb = "yes" if p.get("embeddings_supported") else "no"
        n = p.get("n", 0)
        cold = n < _HEALTH_MIN_N
        sr = p.get("success_rate", 1.0)
        ok_cell = f"{int(sr * 100)}%" if n else "—"
        p50 = p.get("p50_ms", 0)
        p50_cell = f"{p50}ms" if n else "—"
        score = p.get("score", 100.0)
        score_cell = f"{score:.1f}"
        flag = "(cold)" if cold else ""

        cells = [
            p["name"].ljust(widths[0]),
            emb.ljust(widths[1]),
            str(n).ljust(widths[2]),
            ok_cell.ljust(widths[3]),
            p50_cell.ljust(widths[4]),
            score_cell.ljust(widths[5]),
            flag.ljust(widths[6]),
        ]
        line = "".join(cells)
        if not no_color and cold:
            line = f"\033[2m{line}\033[0m"
        lines.append(line)

    # Summary line: count + healthiest non-cold provider.
    warm = [p for p in provs if p.get("n", 0) >= _HEALTH_MIN_N]
    summary = f"\n{len(provs)} provider{'s' if len(provs) != 1 else ''} registered."
    if warm:
        best = max(warm, key=lambda p: p.get("score", 0))
        summary += f" Healthiest: {best['name']} (score {best['score']:.1f})."
    elif provs:
        summary += " All cold — make a few requests to populate health stats."
    lines.append(summary)
    return "\n".join(lines)


def cmd_providers(args) -> int:
    base = args.url.rstrip("/").removesuffix("/v1")
    url = f"{base}/v1/_freeride/providers"
    no_color = bool(args.no_color) or not sys.stdout.isatty()

    try:
        r = httpx.get(url, timeout=5.0)
    except httpx.ConnectError:
        print(
            f"error: gateway not reachable at {args.url}.\n"
            "       start it with `freeride serve` first.",
            file=sys.stderr,
        )
        return 1
    if r.status_code != 200:
        print(f"error: gateway returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 1

    provs = r.json().get("providers", [])
    print(_format_table(provs, no_color=no_color))
    return 0
