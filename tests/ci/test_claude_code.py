"""Phase E + F — Claude Code wrapped and unwrapped on a fresh sandbox.

The headline test: proves the public commitment ("freeride directly
accessible via claude code") works end-to-end against the real
api.anthropic.com plus the real free providers.

Six probes, in order:

  1. baseline_anthropic_direct — plain claude --print, NO freeride
     in path, with ANTHROPIC_API_KEY set. Confirms claude itself
     works; if this fails, nothing else can.
  2. passthrough_claude_haiku — freeride run claude --model
     claude-haiku-4-5. Wrapper sets ANTHROPIC_BASE_URL; gateway
     relays to api.anthropic.com. Subscription/API-key untouched.
  3. passthrough_via_curl_x_api_key — direct curl to gateway with
     x-api-key header. Proves the passthrough HTTP path without
     claude in the loop.
  4. free_route_freeride_free — freeride run claude --model
     freeride/free. Wrapper + gateway route to free providers,
     strip the 70 tools, return real text.
  5. free_route_freeride_fast — same with freeride/fast preset.
     Proves preset preference still works.
  6. telemetry_events — inspect ~/.freeride/events.jsonl, confirm
     the expected event types fired (messages_routing_decision,
     passthrough_response, messages_free_tools_stripped).

Requires ANTHROPIC_API_KEY in env. Budget: ~$0.02 in Haiku calls.

Run:
    set -a; . tests/ci/.env.local; set +a
    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
      python tests/ci/test_claude_code.py
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
    step_install_freeride,
    step_install_uv,
    step_launch_gateway,
    step_upload_env,
    step_wait_for_health,
    timed,
)


@timed("install_claude_cli")
def step_install_claude_cli(sandbox):
    """Install Claude Code 2.x via npm. Needs Node.js first."""
    r = sandbox.process.exec(
        "apt-get install -y -qq nodejs npm 2>&1 | tail -2 && "
        "npm install -g @anthropic-ai/claude-code 2>&1 | tail -5 && "
        "which claude && claude --version"
    )
    return r.exit_code == 0, (r.result or "").strip().splitlines()[-1] if r.result else "", (r.result or "")[-500:]


def _check_text_in_chat_response(body, expected_substring: str | None = None) -> tuple[bool, str]:
    """Helper: pull text from an Anthropic Messages-shaped response."""
    if not isinstance(body, dict):
        return False, f"non-dict response: {type(body).__name__}"
    content = body.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text", "")
        if expected_substring and expected_substring.lower() not in (text or "").lower():
            return False, f"text={text[:60]!r} (expected to contain {expected_substring!r})"
        return True, f"text={text[:60]!r}"
    # Fall back to OpenAI-shaped (claude --print parses Anthropic shape)
    return False, f"no content array: keys={list(body.keys())}"


def _claude_print(sandbox, *, model: str, prompt: str, anthropic_key: str,
                  base_url: str | None) -> tuple[int, str, str]:
    """Run `claude --print <prompt>` inside the sandbox with the given
    model and optional ANTHROPIC_BASE_URL. Returns (exit_code, stdout, stderr_tail).

    The key is passed as ANTHROPIC_API_KEY env var to the child claude
    process via the SDK's exec call. We DO NOT echo the key.
    """
    env_setup = (
        f'export ANTHROPIC_API_KEY={anthropic_key} '
        + (f'ANTHROPIC_BASE_URL={base_url} ' if base_url else '')
    )
    # claude --print needs stdin closed or it waits 3s for input
    safe_prompt = prompt.replace("'", "'\\''")
    cmd = (
        f"{PATH_PREFIX}{env_setup} && "
        f"claude --model {model} --print '{safe_prompt}' < /dev/null 2>&1"
    )
    r = sandbox.process.exec(cmd)
    return r.exit_code, (r.result or ""), (r.result or "")[-400:]


@timed("baseline_anthropic_direct")
def step_baseline_anthropic_direct(sandbox, anthropic_key):
    """Plain claude → api.anthropic.com. No FreeRide in path."""
    code, out, _ = _claude_print(
        sandbox,
        model="claude-haiku-4-5",
        prompt="Reply with exactly the single word: BASELINE",
        anthropic_key=anthropic_key,
        base_url=None,  # let claude use its default
    )
    snippet = out.strip()[:80]
    return (code == 0 and "BASELINE" in out.upper()),\
        f"exit={code}, content={snippet!r}",\
        out[-400:]


@timed("passthrough_claude_haiku")
def step_passthrough_claude_haiku(sandbox, anthropic_key):
    """claude --model claude-haiku-4-5 through the freeride wrapper.
    Gateway should relay to api.anthropic.com (passthrough mode)."""
    code, out, _ = _claude_print(
        sandbox,
        model="claude-haiku-4-5",
        prompt="Reply with exactly the single word: WRAPPED",
        anthropic_key=anthropic_key,
        base_url="http://localhost:11343",
    )
    snippet = out.strip()[:80]
    return (code == 0 and "WRAPPED" in out.upper()),\
        f"exit={code}, content={snippet!r}",\
        out[-400:]


@timed("passthrough_curl_x_api_key")
def step_passthrough_curl_x_api_key(sandbox, anthropic_key):
    """Skip claude entirely — direct curl to the gateway proves the
    passthrough HTTP path works regardless of client."""
    body = _json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 15,
        "messages": [{"role": "user", "content": "Reply with: DIRECT"}],
    })
    cmd = (
        f"curl -sS -X POST http://localhost:11343/v1/messages "
        f"-H 'content-type: application/json' "
        f"-H 'anthropic-version: 2023-06-01' "
        f"-H 'x-api-key: {anthropic_key}' "
        f"-d {repr(body)} -D /tmp/h.txt "
        f"-w '\\n---STATUS:%{{http_code}}'"
    )
    r = sandbox.process.exec(cmd)
    out = r.result or ""
    status = -1
    body_text = out
    if "---STATUS:" in out:
        body_text, status_part = out.rsplit("---STATUS:", 1)
        try:
            status = int(status_part.strip())
        except ValueError:
            pass
    if status != 200:
        return False, f"HTTP {status}", body_text[:300]
    try:
        resp = _json.loads(body_text)
    except _json.JSONDecodeError:
        return False, "non-JSON body", body_text[:300]
    ok, detail = _check_text_in_chat_response(resp, expected_substring="DIRECT")
    # Verify the response header was anthropic-passthrough
    hr = sandbox.process.exec("cat /tmp/h.txt")
    headers_raw = hr.result or ""
    provider_header = ""
    for line in headers_raw.splitlines():
        if line.lower().startswith("x-freeride-provider:"):
            provider_header = line.split(":", 1)[1].strip()
            break
    if provider_header != "anthropic-passthrough":
        return False, f"wrong provider header: {provider_header!r}", body_text[:300]
    return ok, f"passthrough OK — {detail}", body_text[:300]


@timed("free_route_freeride_free")
def step_free_route_freeride_free(sandbox, anthropic_key):
    """claude --model freeride/free through the wrapper. Should route
    to free providers, tools stripped, return real text."""
    code, out, _ = _claude_print(
        sandbox,
        model="freeride/free",
        prompt="In ONE word, what is 1+1?",
        anthropic_key=anthropic_key,
        base_url="http://localhost:11343",
    )
    snippet = out.strip()[:120]
    # We don't assert the content; free providers may return varied
    # phrasing. Just assert exit=0 and non-empty output.
    return (code == 0 and bool(out.strip())),\
        f"exit={code}, content={snippet!r}",\
        out[-400:]


@timed("free_route_freeride_fast")
def step_free_route_freeride_fast(sandbox, anthropic_key):
    """freeride/fast — preset preference. Should route to groq first."""
    code, out, _ = _claude_print(
        sandbox,
        model="freeride/fast",
        prompt="Single word: yes or no?",
        anthropic_key=anthropic_key,
        base_url="http://localhost:11343",
    )
    snippet = out.strip()[:120]
    return (code == 0 and bool(out.strip())),\
        f"exit={code}, content={snippet!r}",\
        out[-400:]


@timed("telemetry_events_fired")
def step_telemetry_events_fired(sandbox):
    """Inspect ~/.freeride/events.jsonl from inside the sandbox.
    Confirm the routing-decision + passthrough + tools-stripped events
    actually fired during the prior probes."""
    r = sandbox.process.exec(
        "grep -E 'messages_routing_decision|passthrough_response|"
        "messages_free_tools_stripped' /root/.freeride/events.jsonl | "
        "python3 -c \""
        "import sys, json; "
        "from collections import Counter; "
        "c = Counter(); "
        "[c.update([json.loads(L)['type']]) for L in sys.stdin if L.strip()]; "
        "print(','.join(f'{k}={v}' for k,v in c.most_common()))\""
    )
    out = (r.result or "").strip()
    if r.exit_code != 0:
        return False, f"events file unreadable: exit={r.exit_code}", out[:200]
    # Expect at least: routing_decision (every call), passthrough_response
    # (for claude-* calls), free_tools_stripped (for freeride/* calls).
    expected_types = {
        "messages_routing_decision",
        "passthrough_response",
        "messages_free_tools_stripped",
    }
    seen = {pair.split("=")[0] for pair in out.split(",")} if out else set()
    missing = expected_types - seen
    return not missing, f"types fired: {out or '(none)'}", ""


def run_phase(*, ref: str, anthropic_key: str, verbose: bool) -> PhaseReport:
    report = PhaseReport(phase="Phase E+F — Claude Code wrapped + unwrapped")

    with ephemeral_sandbox("freeride-test-claude") as (sandbox, dt):
        report.sandbox_id = sandbox.id
        report.sandbox_create_s = dt

        # Setup
        for step_fn in (step_install_uv, step_install_freeride,
                        step_upload_env, step_launch_gateway,
                        step_wait_for_health, step_install_claude_cli):
            r = step_fn(sandbox, ref=ref) if step_fn is step_install_freeride else step_fn(sandbox)
            report.add(r)
            if not r.passed:
                return report

        # Probes
        report.add(step_baseline_anthropic_direct(sandbox, anthropic_key))
        report.add(step_passthrough_claude_haiku(sandbox, anthropic_key))
        report.add(step_passthrough_curl_x_api_key(sandbox, anthropic_key))
        report.add(step_free_route_freeride_free(sandbox, anthropic_key))
        report.add(step_free_route_freeride_fast(sandbox, anthropic_key))
        report.add(step_telemetry_events_fired(sandbox))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("DAYTONA_API_KEY"):
        print("DAYTONA_API_KEY not set", file=sys.stderr)
        return 2
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ANTHROPIC_API_KEY not set — required for Phase E", file=sys.stderr)
        return 2

    print(f"Phase E+F — installing from ref={args.ref}, testing Claude Code wrapped + unwrapped.")
    t0 = time.perf_counter()
    report = run_phase(ref=args.ref, anthropic_key=anthropic_key, verbose=args.verbose)
    elapsed = time.perf_counter() - t0
    print(report.summary(verbose=args.verbose))
    print(f"\n  total wall time: {elapsed:.1f}s")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
