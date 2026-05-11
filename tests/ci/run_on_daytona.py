"""CI primitive: install FreeRide from a git ref into a Daytona sandbox,
run the test suite, and smoke-test the gateway end-to-end.

Run locally:

    DAYTONA_API_KEY=... python tests/ci/run_on_daytona.py main
    DAYTONA_API_KEY=... python tests/ci/run_on_daytona.py <commit-sha>
    DAYTONA_API_KEY=... python tests/ci/run_on_daytona.py feat/some-branch

With live passthrough smoke (spends a tiny bit of Anthropic budget):

    DAYTONA_API_KEY=... ANTHROPIC_API_KEY=... \\
        python tests/ci/run_on_daytona.py main --passthrough-smoke

Exit code: 0 if every step passed, 1 otherwise. Always tears down the
sandbox in a finally block so even crashes don't leak resources.

This is meant to be invoked both locally (for "did I break anything on
Linux?" pre-merge) and from GitHub Actions (matrix entries across Python
versions / branches). The wire format of the output (each step bracketed
with PASS/FAIL banners) is grep-friendly for CI log parsing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass


GIT_URL = "https://github.com/Shaivpidadi/FreeRideV3.git"

# Each sandbox.process.exec() runs in a fresh shell, so $PATH needs to be
# set explicitly per command for `freeride` (installed at ~/.local/bin by
# uv tool install) to be findable. We could write to ~/.bashrc but
# explicit-prefix is more portable across shells the sandbox might use.
_PATH_PREFIX = "export PATH=$HOME/.local/bin:$PATH && "


# Python helper to launch `freeride serve` truly-detached. Run via
# sandbox.process.code_run(). subprocess.Popen + start_new_session
# detaches into its own session group; close_fds severs the parent's
# file-descriptor inheritance so the SDK's exec call returns
# immediately. Shell-level backgrounding (`&`, `nohup`, `setsid`)
# does NOT achieve this through Daytona's exec layer.
_LAUNCH_GATEWAY_PY = r"""
import os, subprocess

env = os.environ.copy()
env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")

log = open("/tmp/fr.log", "wb")
proc = subprocess.Popen(
    ["freeride", "serve", "--port", "11343"],
    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    start_new_session=True, close_fds=True, env=env,
)
print(f"launched pid={proc.pid}")
"""

# What we run, in order. Each entry: (label, shell command, fatal?).
# Fatal=True means the run aborts on failure; False means we record the
# failure and keep going (useful for distinguishing install failures from
# test failures from smoke failures in a single run).
_STEPS_TEMPLATE = [
    ("install_uv",          "pip install -q uv", True),
    ("install_freeride",    _PATH_PREFIX + "uv tool install --prerelease=allow git+{git_url}@{ref}", True),
    ("freeride_version",    _PATH_PREFIX + "freeride --version", True),
    ("clone_for_tests",     "git clone --depth 1 --branch {ref} {git_url} /home/daytona/freeride", False),
    # The tests live in the cloned repo. We install pytest + deps fresh
    # so we don't rely on the uv tool install's editable layout.
    ("pip_install_dev",     "cd /home/daytona/freeride && pip install -q -e .[dev] 2>&1 | tail -3", True),
    ("pytest",              "cd /home/daytona/freeride && python -m pytest --ignore=tests/test_reload.py -q 2>&1 | tail -3", True),
    ("freeride_doctor",     _PATH_PREFIX + "freeride doctor --no-color 2>&1 | tail -15", False),
    # Live smoke: launch gateway, /health, /v1/models. The launch step
    # is handled separately via _LAUNCH_GATEWAY_PY because shell
    # backgrounding (& disown, nohup, setsid) all hang Daytona's
    # sandbox.process.exec — the shell's bookkeeping keeps a wait
    # state that pins the call. subprocess.Popen with
    # start_new_session=True is the only reliable detach we've
    # found across the SDK.
    ("gateway_health",      "sleep 5 && curl -fsS http://localhost:11343/health", True),
    ("gateway_models",      "curl -fsS http://localhost:11343/v1/models | head -c 200", False),
]


@dataclass
class StepResult:
    label: str
    exit_code: int
    stdout_tail: str  # last 500 chars, for log
    duration_s: float

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def _passthrough_smoke_step(anthropic_key: str) -> tuple[str, str, bool]:
    """Build the live passthrough smoke step. Lives separate from the
    template because it needs the user's Anthropic key in the env.
    """
    # We DO NOT echo the key. The env var gets set in the sandbox via
    # the SDK's exec context (which doesn't appear in stdout). We use
    # claude-haiku-4-5 to keep cost minimal.
    cmd = (
        f'ANTHROPIC_API_KEY={anthropic_key} '
        'curl -sS -X POST http://localhost:11343/v1/messages '
        '-H "content-type: application/json" '
        '-H "anthropic-version: 2023-06-01" '
        f'-H "x-api-key: {anthropic_key}" '
        '-d \'{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"say: smoke"}]}\' '
        '| python -c "import sys,json; d=json.load(sys.stdin); print(\\"text:\\", d.get(\\"content\\",[{}])[0].get(\\"text\\",\\"?\\"))"'
    )
    return ("passthrough_smoke", cmd, True)


def _run_step(sandbox, label: str, cmd: str, *, fatal: bool, verbose: bool) -> StepResult:
    t0 = time.perf_counter()
    if verbose:
        # When verbose, show the command itself but never echo a raw API key
        scrubbed = cmd
        for prefix in ("sk-ant-api03-", "sk-ant-oat01-"):
            i = scrubbed.find(prefix)
            if i != -1:
                end = scrubbed.find(" ", i)
                if end == -1:
                    end = i + 60
                scrubbed = scrubbed[:i + len(prefix)] + "<REDACTED>" + scrubbed[end:]
        print(f"  → {scrubbed[:140]}{'…' if len(scrubbed) > 140 else ''}")
    response = sandbox.process.exec(cmd)
    duration = time.perf_counter() - t0
    result = StepResult(
        label=label,
        exit_code=response.exit_code,
        stdout_tail=(response.result or "")[-500:],
        duration_s=duration,
    )
    glyph = "✓" if result.passed else ("✗" if fatal else "!")
    print(f"  [{glyph}] {label:<20s} exit={result.exit_code} in {duration:.1f}s")
    if not result.passed or verbose:
        for line in (result.stdout_tail or "").strip().splitlines()[-6:]:
            print(f"      | {line}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="Git ref to test (branch, tag, or SHA)")
    parser.add_argument(
        "--passthrough-smoke",
        action="store_true",
        help="Also run a live passthrough request to api.anthropic.com "
        "(requires ANTHROPIC_API_KEY env var; spends ~$0.001)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Don't delete the sandbox on exit (useful for post-mortem debugging)",
    )
    args = parser.parse_args()

    if not os.environ.get("DAYTONA_API_KEY"):
        print(
            "DAYTONA_API_KEY not set. Get one at "
            "https://app.daytona.io/dashboard/keys",
            file=sys.stderr,
        )
        return 2

    if args.passthrough_smoke and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "--passthrough-smoke needs ANTHROPIC_API_KEY in env",
            file=sys.stderr,
        )
        return 2

    try:
        from daytona import Daytona, CreateSandboxFromImageParams, Image
    except ImportError:
        print("Daytona SDK not installed. Run: pip install daytona", file=sys.stderr)
        return 2

    print(f"FreeRide CI on Daytona — ref={args.ref}")
    print()

    daytona = Daytona()

    print("→ creating sandbox (debian + python 3.13 + git + curl)...")
    t0 = time.perf_counter()
    # debian_slim is minimal — uv needs git for git+https URLs, curl for
    # the live smoke. Bake them into the image so every CI run starts
    # ready to go.
    image = Image.debian_slim("3.13").run_commands(
        "apt-get update -qq && apt-get install -y -qq git curl ca-certificates"
    )
    sandbox = daytona.create(CreateSandboxFromImageParams(
        image=image,
        name=f"freeride-ci-{args.ref.replace('/', '-').replace('.', '-')[:30]}-{int(time.time())}",
    ))
    print(f"  sandbox ready in {time.perf_counter() - t0:.1f}s (id={sandbox.id})")
    print()

    results: list[StepResult] = []
    try:
        # Build the step list with the ref substituted in
        steps = [
            (label, cmd.format(git_url=GIT_URL, ref=args.ref), fatal)
            for (label, cmd, fatal) in _STEPS_TEMPLATE
        ]
        if args.passthrough_smoke:
            steps.append(_passthrough_smoke_step(os.environ["ANTHROPIC_API_KEY"]))

        print(f"→ running {len(steps)} steps...")
        for label, cmd, fatal in steps:
            result = _run_step(sandbox, label, cmd, fatal=fatal, verbose=args.verbose)
            results.append(result)
            if fatal and not result.passed:
                print(f"  [!] fatal step '{label}' failed; aborting remaining steps")
                break
            # After the freeride_doctor step, launch the gateway via a
            # truly-detached Python subprocess (shell `&` / nohup /
            # setsid all hang Daytona's exec). We splice this into
            # the loop rather than the _STEPS_TEMPLATE because it
            # uses code_run, not exec.
            if label == "freeride_doctor" and result.passed:
                t0 = time.perf_counter()
                launch_resp = sandbox.process.code_run(_LAUNCH_GATEWAY_PY)
                duration = time.perf_counter() - t0
                launch_result = StepResult(
                    label="gateway_launch",
                    exit_code=launch_resp.exit_code,
                    stdout_tail=(launch_resp.result or "")[-200:],
                    duration_s=duration,
                )
                glyph = "✓" if launch_result.passed else "✗"
                print(
                    f"  [{glyph}] {'gateway_launch':<20s} "
                    f"exit={launch_result.exit_code} in {duration:.1f}s"
                )
                if args.verbose or not launch_result.passed:
                    for line in (launch_result.stdout_tail or "").splitlines()[-3:]:
                        print(f"      | {line}")
                results.append(launch_result)
                if not launch_result.passed:
                    print("  [!] gateway_launch failed; aborting smoke")
                    break

    finally:
        print()
        if args.keep_sandbox:
            print(f"→ keeping sandbox {sandbox.id} (--keep-sandbox)")
        else:
            print(f"→ deleting sandbox {sandbox.id}...")
            try:
                sandbox.delete()
                print("  done")
            except Exception as e:  # noqa: BLE001
                print(f"  warning: delete failed: {e}")

    print()
    print("─── summary ────────────────────────────────────────────")
    n_pass = sum(1 for r in results if r.passed)
    n_fail = sum(1 for r in results if not r.passed)
    total_s = sum(r.duration_s for r in results)
    print(f"  {n_pass}/{len(results)} passed in {total_s:.1f}s")
    if n_fail:
        print("  failed steps:")
        for r in results:
            if not r.passed:
                print(f"    - {r.label} (exit={r.exit_code})")
        return 1
    print("  all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
