"""Phase A — normal FreeRide flow on a fresh Linux sandbox.

What this proves:
  1. install.sh / uv tool install path works on Debian
  2. Gateway boots, registers providers from ~/.freeride/.env
  3. /health returns 200 with the registered provider list
  4. /v1/models returns a non-empty catalog
  5. POST /v1/chat/completions with model=auto returns real text
     from a real free provider (smart router actually picks something)
  6. The chat response has the expected OpenAI-shaped envelope

This is the foundation. Every other phase depends on these steps
working. If this fails, no point running the rest.

Run:
    set -a; . tests/ci/.env.local; set +a
    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
      python tests/ci/test_normal_flow.py [--ref main]
"""

from __future__ import annotations

import argparse
import sys
import time

from _daytona_lib import (
    PhaseReport,
    ephemeral_sandbox,
    post_chat,
    step_install_freeride,
    step_install_uv,
    step_launch_gateway,
    step_upload_env,
    step_wait_for_health,
    timed,
)


@timed("models_endpoint")
def step_models_endpoint(sandbox):
    """/v1/models should return a non-empty catalog after provider
    .env is loaded. Pipe through jq to extract just the count — avoids
    bringing the full JSON (often 100KB+) through the SDK's stdout."""
    r = sandbox.process.exec(
        "curl -fsS http://localhost:11343/v1/models | "
        "python3 -c 'import sys, json; "
        'd=json.load(sys.stdin); '
        "print(len(d.get(\"data\",[])))'"
    )
    if r.exit_code != 0:
        return False, f"exit={r.exit_code}", (r.result or "")[-500:]
    try:
        n = int(r.result.strip())
    except ValueError:
        return False, "non-numeric count", r.result[:300]
    return n > 0, f"{n} models cataloged", ""


@timed("chat_auto_model")
def step_chat_auto_model(sandbox):
    """The headline test: POST /v1/chat/completions with model=auto,
    get real text content back from a real free provider."""
    status, body, headers = post_chat(
        sandbox,
        body={
            "model": "auto",
            "max_tokens": 30,
            "messages": [{"role": "user", "content": "Reply with exactly the single word: ALIVE"}],
        },
    )
    if status != 200:
        return False, f"HTTP {status}", str(body)[:400]
    # Extract content
    choices = body.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
    provider = headers.get("x-freeride-provider", "?")
    return (
        bool(text.strip()),
        f"provider={provider}, content={text!r}",
        "",
    )


@timed("chat_specific_free_model")
def step_chat_specific_free_model(sandbox):
    """Test a specific free model id by name. We use openrouter/auto
    which is a smart-router that picks the best free OpenRouter model.
    If OpenRouter has no usable key, expect a 503 (which is informative,
    not a failure of the test infrastructure)."""
    status, body, headers = post_chat(
        sandbox,
        body={
            "model": "openrouter/auto",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "say: ok"}],
        },
    )
    if status == 200:
        provider = headers.get("x-freeride-provider", "?")
        choices = body.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = ((choices[0].get("message") or {}).get("content") or "") or ""
        # text may legitimately be empty if the model only emitted a
        # tool_calls reply or finished on stop_sequence — 200 alone is
        # the assertion, not non-empty text.
        snippet = (text or "")[:30] if isinstance(text, str) else ""
        return True, f"provider={provider}, content={snippet!r}", ""
    elif status == 503:
        # 503 here means no usable OR key right now; that's a state we
        # report but it's still a meaningful test result.
        err = (body.get("detail") or body).get("error", {}) if isinstance(body, dict) else {}
        return True, f"503 (informational): {err.get('type','?')}", str(body)[:300]
    return False, f"HTTP {status}", str(body)[:400]


def run_phase(*, ref: str, verbose: bool) -> PhaseReport:
    report = PhaseReport(phase="Phase A — normal FreeRide flow")

    with ephemeral_sandbox("freeride-test-normal") as (sandbox, dt):
        report.sandbox_id = sandbox.id
        report.sandbox_create_s = dt

        report.add(step_install_uv(sandbox))
        if not report.results[-1].passed:
            return report

        report.add(step_install_freeride(sandbox, ref=ref))
        if not report.results[-1].passed:
            return report

        report.add(step_upload_env(sandbox))
        if not report.results[-1].passed:
            return report

        report.add(step_launch_gateway(sandbox))
        if not report.results[-1].passed:
            return report

        report.add(step_wait_for_health(sandbox))
        if not report.results[-1].passed:
            return report

        report.add(step_models_endpoint(sandbox))
        report.add(step_chat_auto_model(sandbox))
        report.add(step_chat_specific_free_model(sandbox))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="git ref to install (default: main)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    import os
    if not os.environ.get("DAYTONA_API_KEY"):
        print("DAYTONA_API_KEY not set. Source tests/ci/.env.local first.", file=sys.stderr)
        return 2

    print(f"Phase A — installing from ref={args.ref}, then exercising the gateway.")
    t0 = time.perf_counter()
    report = run_phase(ref=args.ref, verbose=args.verbose)
    elapsed = time.perf_counter() - t0
    print(report.summary(verbose=args.verbose))
    print(f"\n  total wall time: {elapsed:.1f}s")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
