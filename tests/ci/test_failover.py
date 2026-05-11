"""Phase C — failover behavior.

Inject an invalid key for the highest-priority provider (OpenRouter,
which is "always-on" / first in the chain). Send a normal chat
request. Confirm:
  1. Response is 200 — gateway didn't bubble up OR's 401
  2. X-FreeRide-Provider is NOT openrouter — failover landed elsewhere
  3. Telemetry shows OpenRouter was attempted and failed before the
     successful provider was tried

This proves the failover machinery actually works under a real-world
condition (revoked key, expired key, etc.). Without this we'd ship
v0 features that LOOK fine in unit tests but blow up on real
provider state.

Run:
    set -a; . tests/ci/.env.local; set +a
    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
      python tests/ci/test_failover.py
"""

from __future__ import annotations

import argparse
import json as _json
import os
import sys
import time

from _daytona_lib import (
    PATH_PREFIX,
    PhaseReport,
    StepResult,
    ephemeral_sandbox,
    post_chat,
    step_install_freeride,
    step_install_uv,
    step_launch_gateway,
    step_upload_env,
    step_wait_for_health,
    timed,
)


@timed("install_invalid_openrouter_env")
def step_install_invalid_openrouter_env(sandbox):
    """Overwrite OPENROUTER_API_KEY in the sandbox's .env with an
    obviously-invalid value so OR returns 401 on every request.

    The other 5 providers keep their valid keys from the .env we
    uploaded. We do this AFTER the regular upload_env step so we're
    starting from a known-good state and only mutating one variable.
    """
    r = sandbox.process.exec(
        # Replace the OPENROUTER_API_KEY line with a clearly-bogus value
        "sed -i 's|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=sk-or-v1-INVALID-FAILOVER-TEST|' "
        "/root/.freeride/.env && "
        "grep '^OPENROUTER_API_KEY=' /root/.freeride/.env"
    )
    # The grep echoes the (now-invalid) key, but it's a known fake.
    # Don't include it in detail.
    return r.exit_code == 0, "OPENROUTER_API_KEY swapped to invalid sentinel", ""


@timed("restart_gateway_with_invalid_or_key")
def step_restart_gateway_with_invalid_or_key(sandbox):
    """The gateway picks up env vars at boot. After mutating .env we
    need to restart so the new (invalid) OR key takes effect."""
    sandbox.process.exec("pkill -f 'freeride.*serve' 2>/dev/null; sleep 1")
    # Use the same launch primitive
    from _daytona_lib import LAUNCH_GATEWAY_PY
    r = sandbox.process.code_run(LAUNCH_GATEWAY_PY)
    return r.exit_code == 0, "", (r.result or "")[-200:]


@timed("chat_with_failover")
def step_chat_with_failover(sandbox):
    """Send a chat request that SHOULD fail OR first then succeed on
    the next provider. Use a Groq-known model since OR has an invalid
    key. Force-route is NOT used — we want the normal failover flow."""
    t0 = time.perf_counter()
    status, body, headers = post_chat(
        sandbox,
        body={
            "model": "llama-3.3-70b-versatile",  # Groq's known model id
            "max_tokens": 12,
            "messages": [{"role": "user", "content": "say: failed-over"}],
        },
    )
    if status != 200:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        return False, f"HTTP {status} — failover did NOT recover", _json.dumps(detail)[:300]
    provider = headers.get("x-freeride-provider", "?")
    if provider == "openrouter":
        return False, f"served via openrouter — invalid key not detected", ""
    choices = body.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("message") or {}).get("content") or ""
    text = text if isinstance(text, str) else ""
    return True, f"failover landed on {provider!r}, content={text[:30]!r}", ""


_TELEMETRY_PROBE_PY = r"""
import json

events = [json.loads(L) for L in open('/root/.freeride/events.jsonl') if L.strip()]
last_req = next((e for e in reversed(events) if e.get('type') == 'request_start'), None)
rid = last_req.get('request_id') if last_req else None
matched = [e for e in events if e.get('request_id') == rid]
print(f'request_id: {rid}')
print(f'events for this request: {len(matched)}')
for e in matched:
    print(f'  {e["type"]:25s} provider={e.get("provider","-"):15s} status={e.get("status","-")}')
"""


@timed("telemetry_shows_failover_attempts")
def step_telemetry_shows_failover_attempts(sandbox):
    """Dump the most recent request's event trail. Confirms OR was
    attempted, failed, and the gateway tried a different provider
    that succeeded.

    Uses code_run (not exec + inline -c) because the dump includes a
    for-loop that needs real newlines.
    """
    r = sandbox.process.code_run(_TELEMETRY_PROBE_PY)
    out = (r.result or "").strip()
    if r.exit_code != 0:
        return False, f"code_run failed: exit={r.exit_code}", out[:300]
    # Look for failover pattern: OR appears with non-ok status, and a
    # different provider appears with status=ok.
    # Note: event status field is uppercase 'OK' in the gateway's
    # telemetry emit; lowercase 'ok' was a guess. Match both.
    lines = [ln.lower() for ln in out.splitlines()]
    or_failed = any("provider=openrouter" in ln and "status=ok" not in ln
                    and "provider_response" in ln
                    for ln in lines)
    non_or_ok = any(
        "status=ok" in ln and "provider=openrouter" not in ln
        and "provider=-" not in ln
        and "provider_response" in ln
        for ln in lines
    )
    return (
        or_failed and non_or_ok,
        f"OR failed: {or_failed}, non-OR succeeded: {non_or_ok}",
        out,
    )


def run_phase(*, ref: str, verbose: bool) -> PhaseReport:
    report = PhaseReport(phase="Phase C — failover behavior")

    with ephemeral_sandbox("freeride-test-failover") as (sandbox, dt):
        report.sandbox_id = sandbox.id
        report.sandbox_create_s = dt

        for step_fn in (step_install_uv, step_install_freeride,
                        step_upload_env, step_launch_gateway,
                        step_wait_for_health):
            r = step_fn(sandbox, ref=ref) if step_fn is step_install_freeride else step_fn(sandbox)
            report.add(r)
            if not r.passed:
                return report

        # Now break OpenRouter's key and restart the gateway
        report.add(step_install_invalid_openrouter_env(sandbox))
        if not report.results[-1].passed:
            return report

        report.add(step_restart_gateway_with_invalid_or_key(sandbox))
        if not report.results[-1].passed:
            return report

        # Give gateway a moment to come back up
        report.add(step_wait_for_health(sandbox))
        if not report.results[-1].passed:
            return report

        # The real test
        report.add(step_chat_with_failover(sandbox))
        report.add(step_telemetry_shows_failover_attempts(sandbox))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("DAYTONA_API_KEY"):
        print("DAYTONA_API_KEY not set", file=sys.stderr)
        return 2

    print(f"Phase C — testing failover with invalid OpenRouter key, ref={args.ref}.")
    t0 = time.perf_counter()
    report = run_phase(ref=args.ref, verbose=args.verbose)
    elapsed = time.perf_counter() - t0
    print(report.summary(verbose=args.verbose))
    print(f"\n  total wall time: {elapsed:.1f}s")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
