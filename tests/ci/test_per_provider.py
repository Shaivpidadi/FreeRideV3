"""Phase B — each provider individually.

For every registered provider, force-route to it via
``X-FreeRide-Force-Provider`` and confirm a real chat call returns
200 with content. Records per-provider status so we know which
providers are healthy today and which are rate-limited/down.

Forcing the provider bypasses the smart-router and per-key health
ranking — we want a direct yes/no for each one. A failure here is
ALWAYS reported as a finding (the gateway is working as intended,
the provider just isn't), not as a test infrastructure failure.

Run:
    set -a; . tests/ci/.env.local; set +a
    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
      python tests/ci/test_per_provider.py
"""

from __future__ import annotations

import argparse
import sys
import time

from _daytona_lib import (
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


# Providers we expect to attempt. Order = display order; the gateway
# registers all of them when env vars are set, but some may not have
# keys in the user's .env or may be unreachable from the sandbox.
PROVIDERS = [
    "openrouter",
    "groq",
    "huggingface",
    "cerebras",
    "nvidia_nim",
    "ollama",
]


def _get_catalog_by_provider(sandbox) -> dict[str, list[str]]:
    """Return ``{provider_name: [model_id, ...]}`` from /v1/models.

    Providers without entries (groq has no public model-list endpoint,
    ollama has no daemon in sandbox) are absent here. The caller decides
    whether that's a test failure or expected behavior.
    """
    r = sandbox.process.exec(
        "curl -fsS http://localhost:11343/v1/models | "
        "python3 -c \""
        "import sys, json; "
        "from collections import defaultdict; "
        "data = json.load(sys.stdin)['data']; "
        "by = defaultdict(list); "
        "[by[m.get('owned_by','?')].append(m['id']) for m in data]; "
        "print(json.dumps(by))\""
    )
    if r.exit_code != 0 or not r.result:
        return {}
    import json as _json
    try:
        return _json.loads(r.result.strip())
    except _json.JSONDecodeError:
        return {}


# Hardcoded fallbacks for providers that don't appear in /v1/models
# but accept model ids directly. Update when these change.
_PROVIDER_FALLBACK_MODELS = {
    "groq": "llama-3.3-70b-versatile",  # current Groq Llama free tier
}


def _probe_provider(sandbox, provider_name: str, catalog: dict[str, list[str]]) -> StepResult:
    """Send a tiny chat call forcing one provider; classify the result."""
    # Pick a model: catalog first, then hardcoded fallback
    model_ids = catalog.get(provider_name) or []
    if model_ids:
        model_id = model_ids[0]
        model_source = "catalog"
    elif provider_name in _PROVIDER_FALLBACK_MODELS:
        model_id = _PROVIDER_FALLBACK_MODELS[provider_name]
        model_source = "fallback"
    else:
        # ollama in sandbox (no daemon) — no point probing
        return StepResult(
            label=f"provider:{provider_name}",
            passed=False,
            duration_s=0.0,
            detail="SKIPPED — not in /v1/models catalog and no known fallback model",
        )

    t0 = time.perf_counter()
    status, body, headers = post_chat(
        sandbox,
        body={
            "model": model_id,
            "max_tokens": 12,
            "messages": [{"role": "user", "content": "say: ok"}],
        },
        headers={"X-FreeRide-Force-Provider": provider_name},
    )
    duration = time.perf_counter() - t0
    actual_provider = headers.get("x-freeride-provider", "?")

    if status == 200:
        choices = body.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = ((choices[0].get("message") or {}).get("content") or "") or ""
        text = text if isinstance(text, str) else ""
        # The force header must be honored — provider returned MUST
        # match what we asked for. If not, that's a real bug.
        if actual_provider != provider_name:
            return StepResult(
                label=f"provider:{provider_name}",
                passed=False,
                duration_s=duration,
                detail=f"FORCE BYPASSED — gateway served via {actual_provider!r} instead",
            )
        return StepResult(
            label=f"provider:{provider_name}",
            passed=True,
            duration_s=duration,
            detail=f"200 [{model_source}:{model_id[:30]}] — {text[:20]!r}",
        )

    # Non-200: classify. 400 with force_provider_unknown means the
    # provider wasn't registered (env var missing). 503 means provider
    # had no usable key / all keys cooling / model rejected.
    detail_obj = body.get("detail", {}) if isinstance(body, dict) else {}
    if not isinstance(detail_obj, dict):
        detail_obj = {}
    err = detail_obj.get("error", {})
    if not isinstance(err, dict):
        err = {}
    err_type = err.get("type") or detail_obj.get("error_type") or "?"
    err_msg = err.get("message") or detail_obj.get("message") or ""
    # Fall back to a raw body snippet if structured parsing yielded
    # nothing — we want to see WHY 5xx fired.
    if err_type == "?" and not err_msg:
        import json as _json
        try:
            err_msg = _json.dumps(body)[:140]
        except (TypeError, ValueError):
            err_msg = str(body)[:140]

    if status == 400 and err_type == "force_provider_unknown":
        return StepResult(
            label=f"provider:{provider_name}",
            passed=False,
            duration_s=duration,
            detail=f"NOT REGISTERED (env var missing) — {err_type}",
        )

    # 503 / 5xx — provider tried but failed. Report the upstream error
    # class + a snippet of the body. Not a test infrastructure failure;
    # it's a provider-state finding worth surfacing for triage.
    return StepResult(
        label=f"provider:{provider_name}",
        passed=False,
        duration_s=duration,
        detail=f"HTTP {status} [{model_source}:{model_id[:25]}] {err_type}: {err_msg[:100]}",
    )


@timed("registered_providers_match_expected")
def step_check_registered(sandbox):
    """Compare /v1/_freeride/providers output against expected list."""
    r = sandbox.process.exec(
        "curl -fsS http://localhost:11343/v1/_freeride/providers | "
        "python3 -c \"import sys,json; d=json.load(sys.stdin); "
        "print(','.join(sorted(p['name'] for p in d['providers'])))\""
    )
    got = (r.result or "").strip()
    expected = ",".join(sorted(PROVIDERS))
    return (
        got == expected,
        f"got={got}",
        "",
    )


def run_phase(*, ref: str, verbose: bool) -> PhaseReport:
    report = PhaseReport(phase="Phase B — per-provider live tests")

    with ephemeral_sandbox("freeride-test-providers") as (sandbox, dt):
        report.sandbox_id = sandbox.id
        report.sandbox_create_s = dt

        # Boilerplate setup
        for step_fn in (step_install_uv, step_install_freeride,
                        step_upload_env, step_launch_gateway,
                        step_wait_for_health):
            r = step_fn(sandbox) if step_fn is not step_install_freeride \
                else step_fn(sandbox, ref=ref)
            report.add(r)
            if not r.passed:
                return report

        # Verify the registered list matches what we expect
        report.add(step_check_registered(sandbox))

        # Build the catalog once so we know which model id to send for each
        # provider. Providers without catalog entries fall back to a
        # hardcoded model id (or are skipped).
        catalog = _get_catalog_by_provider(sandbox)
        catalog_summary = ", ".join(f"{k}={len(v)}" for k, v in sorted(catalog.items())) or "(empty)"
        report.add(StepResult(
            label="catalog_per_provider",
            passed=bool(catalog),
            duration_s=0.0,
            detail=catalog_summary,
        ))

        # Now: probe each provider in turn. We intentionally do NOT
        # stop on first failure here — we want the full per-provider
        # picture.
        for provider in PROVIDERS:
            report.add(_probe_provider(sandbox, provider, catalog))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="git ref to install")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    import os
    if not os.environ.get("DAYTONA_API_KEY"):
        print("DAYTONA_API_KEY not set. Source tests/ci/.env.local first.",
              file=sys.stderr)
        return 2

    print(f"Phase B — installing from ref={args.ref}, probing each provider individually.")
    t0 = time.perf_counter()
    report = run_phase(ref=args.ref, verbose=args.verbose)
    elapsed = time.perf_counter() - t0
    print(report.summary(verbose=args.verbose))
    print(f"\n  total wall time: {elapsed:.1f}s")

    # Phase B exit code: only fatal if SETUP failed. Per-provider
    # failures are informative, not blocking. We return 0 if the
    # setup steps all passed and we got per-provider data back.
    setup_steps = [r for r in report.results if not r.label.startswith("provider:")]
    setup_ok = all(r.passed for r in setup_steps)
    return 0 if setup_ok else 1


if __name__ == "__main__":
    sys.exit(main())
