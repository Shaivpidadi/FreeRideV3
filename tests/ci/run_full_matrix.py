"""Run all 5 CI phase scripts in parallel against fresh Daytona sandboxes.

This is the "test everything" command. Each phase runs in its own
sandbox (no contamination between phases) and concurrently (total
wall time bounded by the slowest single phase, not the sum).

Per-phase wall times observed:
  Phase A — normal flow                ~25s
  Phase B — per-provider               ~15s
  Phase C — failover                   ~15s
  Phase D — binders                    ~65s
  Phase E+F — Claude Code              ~150s (claude install is 75s)

Parallel total: ~150s (bounded by E+F). Sequential would be ~270s.

Usage:
    set -a; . tests/ci/.env.local; set +a
    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
      python tests/ci/run_full_matrix.py [--ref main]

Exit code is the worst across all phases (0 if all green, 1 if any
phase reports a fatal failure).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import time


PHASES = [
    ("Phase A (normal flow)",       "test_normal_flow.py"),
    ("Phase B (per-provider)",      "test_per_provider.py"),
    ("Phase C (failover)",          "test_failover.py"),
    ("Phase D (binders)",           "test_binders.py"),
    ("Phase E+F (claude code)",     "test_claude_code.py"),
]


def _run_phase(phase_name: str, script: str, ref: str, extra_args: list[str]) -> tuple[str, int, str, float]:
    """Run one phase script as a subprocess, return (name, exit_code,
    output, duration). Output is the full stdout+stderr — captured so
    parallel runs don't interleave their lines."""
    t0 = time.perf_counter()
    script_path = os.path.join(os.path.dirname(__file__), script)
    cmd = [sys.executable, "-u", script_path, "--ref", ref] + extra_args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1200,  # 20 min per phase ceiling
        )
        out = proc.stdout + ("\n--STDERR--\n" + proc.stderr if proc.stderr.strip() else "")
        return phase_name, proc.returncode, out, time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        return phase_name, 124, f"<TIMEOUT after 20 min>\n", time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        return phase_name, 1, f"<orchestrator raised {type(e).__name__}: {e}>\n", time.perf_counter() - t0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each phase'\''s full output (default: just the summary)")
    parser.add_argument("--max-parallel", type=int, default=5,
                        help="Maximum concurrent phases (default: all 5)")
    args = parser.parse_args()

    if not os.environ.get("DAYTONA_API_KEY"):
        print("DAYTONA_API_KEY not set — source tests/ci/.env.local first.",
              file=sys.stderr)
        return 2

    extra = ["-v"] if args.verbose else []

    print(f"FreeRide full matrix — {len(PHASES)} phases against fresh Daytona "
          f"sandboxes, ref={args.ref}")
    print(f"  parallel: up to {args.max_parallel} concurrent")
    print()

    t0 = time.perf_counter()
    results: list[tuple[str, int, str, float]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as ex:
        futures = {
            ex.submit(_run_phase, name, script, args.ref, extra): name
            for name, script in PHASES
        }
        # As each phase finishes, print its tail line so we have live
        # progress instead of going silent for 2+ minutes.
        for fut in concurrent.futures.as_completed(futures):
            name, code, out, dt = fut.result()
            results.append((name, code, out, dt))
            glyph = "✓" if code == 0 else "✗"
            print(f"  [{glyph}] {name:<28s} exit={code} in {dt:.1f}s")
    total_elapsed = time.perf_counter() - t0

    print()
    print("─── summary ────────────────────────────────────────────")
    n_pass = sum(1 for _, c, *_ in results if c == 0)
    print(f"  {n_pass}/{len(results)} phases green")
    for name, code, out, dt in sorted(results, key=lambda r: r[3]):
        # Pull the last "all green" / "X failed" line from each phase output
        verdict = "?"
        for line in (out or "").splitlines():
            if "all green" in line:
                verdict = "✓"
                break
            if "failed" in line and "✗" in line:
                verdict = line.strip()[:60]
                break
        print(f"  {name:<28s} {dt:>6.1f}s  {verdict}")
    print()
    print(f"  total wall time: {total_elapsed:.1f}s")

    if args.verbose:
        for name, code, out, _ in results:
            print(f"\n═══ {name} (exit={code}) ═══════════════════════════")
            print(out)

    return 0 if all(c == 0 for _, c, *_ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
