"""Shared infrastructure for tests/ci/test_*.py phase scripts.

Encapsulates the boilerplate that every phase needs:
  - sandbox lifecycle (create + always-delete via context manager)
  - freeride install from a git ref
  - .env upload (provider keys)
  - gateway launch + health wait
  - structured step results

Each phase script imports from here and writes only its phase-specific
probes. Keeps the per-phase script focused on what it's actually testing.

Run any phase locally:

    SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())") \\
    DAYTONA_API_KEY=... python tests/ci/test_<phase>.py [args]
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


GIT_URL = "https://github.com/Shaivpidadi/FreeRideV3.git"

# Path prefix needed because uv tool installs to ~/.local/bin which
# isn't on debian-slim's default PATH.
PATH_PREFIX = "export PATH=$HOME/.local/bin:$PATH && "


# Default image: debian-slim with git + curl baked in. uv tool install
# from a git URL needs git; live smoke needs curl.
def default_image():
    from daytona import Image

    return Image.debian_slim("3.13").run_commands(
        "apt-get update -qq && apt-get install -y -qq git curl ca-certificates"
    )


# Truly-detached gateway launch. Shell backgrounding (&, nohup, setsid)
# all hang Daytona's sandbox.process.exec because descriptor inheritance
# keeps the SDK call blocked. subprocess.Popen + start_new_session +
# close_fds is the only pattern that returns immediately.
LAUNCH_GATEWAY_PY = r"""
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


# ─── result types ───────────────────────────────────────────────────


@dataclass
class StepResult:
    label: str
    passed: bool
    duration_s: float
    detail: str = ""        # short human-readable summary
    stdout_tail: str = ""   # last ~500 chars of stdout for debug

    def render(self, verbose: bool = False) -> str:
        glyph = "✓" if self.passed else "✗"
        out = f"  [{glyph}] {self.label:<28s} in {self.duration_s:.1f}s"
        if self.detail:
            out += f"  — {self.detail}"
        if (not self.passed or verbose) and self.stdout_tail:
            for line in self.stdout_tail.strip().splitlines()[-6:]:
                out += f"\n      | {line}"
        return out


@dataclass
class PhaseReport:
    phase: str
    sandbox_id: str = ""
    results: list[StepResult] = field(default_factory=list)
    sandbox_create_s: float = 0.0

    def add(self, result: StepResult) -> None:
        self.results.append(result)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def summary(self, verbose: bool = False) -> str:
        lines = [f"\n── {self.phase} ─────────────────────────────────────"]
        if self.sandbox_id:
            lines.append(f"  sandbox: {self.sandbox_id} (created in {self.sandbox_create_s:.1f}s)")
        for r in self.results:
            lines.append(r.render(verbose=verbose))
        n_pass = sum(1 for r in self.results if r.passed)
        verdict = "✓ all green" if self.passed else f"✗ {len(self.results) - n_pass} failed"
        lines.append(f"  {verdict} ({n_pass}/{len(self.results)})")
        return "\n".join(lines)


# ─── sandbox lifecycle ──────────────────────────────────────────────


@contextmanager
def ephemeral_sandbox(name_prefix: str, *, image=None):
    """Create a sandbox, yield (sandbox, create_duration_s), always delete on exit.

    Usage:
        with ephemeral_sandbox("freeride-test-foo") as (sandbox, dt):
            ...
    """
    from daytona import Daytona, CreateSandboxFromImageParams

    daytona = Daytona()
    t0 = time.perf_counter()
    sandbox = daytona.create(CreateSandboxFromImageParams(
        image=image or default_image(),
        name=f"{name_prefix}-{int(time.time())}",
    ))
    dt = time.perf_counter() - t0
    try:
        yield sandbox, dt
    finally:
        try:
            sandbox.delete()
        except Exception:  # noqa: BLE001
            pass


# ─── timed step runner ──────────────────────────────────────────────


def timed(label: str):
    """Decorator factory: wrap a function returning (passed, detail, stdout_tail)
    so the resulting StepResult is annotated with duration. Usage:

        @timed("install_freeride")
        def step_install(sandbox, ref):
            resp = sandbox.process.exec(...)
            return resp.exit_code == 0, "", resp.result[-500:]

        result = step_install(sandbox, "main")
    """
    def deco(fn):
        def wrapped(*args, **kwargs) -> StepResult:
            t0 = time.perf_counter()
            try:
                passed, detail, tail = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                return StepResult(
                    label=label,
                    passed=False,
                    duration_s=time.perf_counter() - t0,
                    detail=f"raised {type(e).__name__}: {e}",
                )
            return StepResult(
                label=label,
                passed=passed,
                duration_s=time.perf_counter() - t0,
                detail=detail,
                stdout_tail=tail,
            )
        return wrapped
    return deco


# ─── common steps reused across phases ──────────────────────────────


@timed("install_uv")
def step_install_uv(sandbox):
    r = sandbox.process.exec("pip install -q uv 2>&1 | tail -3")
    return r.exit_code == 0, "", (r.result or "")[-500:]


@timed("install_freeride")
def step_install_freeride(sandbox, ref: str = "main"):
    r = sandbox.process.exec(
        f"{PATH_PREFIX}uv tool install --prerelease=allow git+{GIT_URL}@{ref} 2>&1 | tail -5"
    )
    return r.exit_code == 0, f"ref={ref}", (r.result or "")[-500:]


@timed("upload_env")
def step_upload_env(sandbox, local_env_path: str | None = None):
    """Upload ~/.freeride/.env (or a custom path) into the sandbox.

    Daytona sandboxes run code as root (HOME=/root), not as a
    /home/daytona user. We detect the runtime HOME in the sandbox
    and upload there so freeride's ``load_dotenv_into_environ()``
    (which reads ``Path.home() / ".freeride" / ".env"``) actually
    finds the file.

    The bytes never appear in stdout. We chmod 600 in the sandbox so
    the file isn't world-readable inside that env.
    """
    src = Path(local_env_path or os.path.expanduser("~/.freeride/.env"))
    if not src.exists():
        return False, f"local .env not found at {src}", ""
    contents = src.read_bytes()

    # Detect runtime HOME in the sandbox — don't hardcode /home/daytona.
    home_resp = sandbox.process.exec('printf "%s" "$HOME"')
    home = (home_resp.result or "").strip() or "/root"

    sandbox.process.exec(f"mkdir -p {home}/.freeride")
    sandbox.fs.upload_file(contents, f"{home}/.freeride/.env")
    sandbox.process.exec(f"chmod 600 {home}/.freeride/.env")

    # Record only the var names, not values, in the detail field
    names = []
    for line in contents.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0])
    return True, f"uploaded to {home}/.freeride/.env ({len(names)} vars: {', '.join(names)})", ""


@timed("launch_gateway")
def step_launch_gateway(sandbox):
    r = sandbox.process.code_run(LAUNCH_GATEWAY_PY)
    return r.exit_code == 0, "", (r.result or "")[-200:]


@timed("wait_for_health")
def step_wait_for_health(sandbox, timeout_s: float = 15.0):
    """Poll /health from inside the sandbox. We do the polling there to
    avoid Daytona-tunnel latency in the wait loop."""
    deadline_s = int(timeout_s)
    cmd = (
        f"for i in $(seq 1 {deadline_s * 2}); do "
        '  if curl -fsS http://localhost:11343/health 2>/dev/null; then exit 0; fi; '
        '  sleep 0.5; '
        "done; exit 1"
    )
    r = sandbox.process.exec(cmd)
    ok = r.exit_code == 0
    detail = ""
    if ok:
        try:
            data = json.loads(r.result.strip())
            detail = f"providers={','.join(data.get('providers') or [])}"
        except (ValueError, AttributeError):
            detail = "200"
    return ok, detail, (r.result or "")[-500:]


# ─── helper: run a chat or messages call inside the sandbox ─────────


def post_chat(sandbox, *, body: dict, headers: dict | None = None,
              endpoint: str = "/v1/chat/completions") -> tuple[int, dict, dict]:
    """POST a request from inside the sandbox, return (status, response_body, response_headers).

    Doing it in-sandbox avoids exposing the gateway to the public internet
    and avoids Daytona tunnel cold-start.
    """
    h = dict(headers or {})
    h.setdefault("content-type", "application/json")
    header_args = " ".join(f'-H {repr(f"{k}: {v}")}' for k, v in h.items())
    cmd = (
        f"curl -sS -X POST http://localhost:11343{endpoint} "
        f"{header_args} "
        f"-D /tmp/h.txt "
        f"-d {repr(json.dumps(body))} "
        f"-w '\\n---STATUS:%{{http_code}}'"
    )
    r = sandbox.process.exec(cmd)
    out = r.result or ""
    status = 0
    body_text = out
    if "---STATUS:" in out:
        body_text, status_part = out.rsplit("---STATUS:", 1)
        try:
            status = int(status_part.strip())
        except ValueError:
            status = -1
    try:
        body_json = json.loads(body_text)
    except (ValueError, TypeError):
        body_json = {"_raw": body_text[:500]}
    # Pull response headers
    headers_resp = {}
    hr = sandbox.process.exec("cat /tmp/h.txt 2>/dev/null")
    for line in (hr.result or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers_resp[k.strip().lower()] = v.strip()
    return status, body_json, headers_resp
