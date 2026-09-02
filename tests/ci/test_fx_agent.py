"""Daytona phase: the fx/ridex agent path, end to end on a clean sandbox.

Proves the two greens from internal-docs/RIDEX_PLAN.md against a real
install and real free-tier providers:

  first green   GET /coding-agent/v1/models answers on a dummy Bearer,
                and a pong prompt through POST /v3/ai/language-model
                (fx gateway dialect) streams text back.
  second green  a Write-tool request comes back as a well-formed
                fx ``tool-call`` event with parseable JSON input —
                FreeRide's half of the tool round-trip.

Usage:
    set -a; . tests/ci/.env.local; set +a
    export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")
    python tests/ci/test_fx_agent.py [--ref cli] [--env-file /path/.env]
"""

from __future__ import annotations

import argparse
import json
import shlex

from _daytona_lib import (
    PhaseReport,
    ephemeral_sandbox,
    step_install_freeride,
    step_install_uv,
    step_launch_gateway,
    step_upload_env,
    step_wait_for_health,
    timed,
)

PONG_BODY = {
    "prompt": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "reply with the single word pong"}],
        }
    ],
    "tools": [],
    "toolChoice": {"type": "auto"},
    # Generous on purpose: the coding route can resolve to reasoning
    # models (observed: openai/gpt-oss-120b) that spend budget thinking
    # before the first text token — 16 produced finish=length and no text.
    "maxOutputTokens": 256,
}

WRITE_TOOL_BODY = {
    "prompt": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Create a file named hello.txt containing exactly: hi",
                }
            ],
        }
    ],
    "tools": [
        {
            "type": "function",
            "name": "Write",
            "description": "Write content to a file at the given path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }
    ],
    "toolChoice": {"type": "auto"},
    "maxOutputTokens": 512,
}


def _curl_fx(body: dict, timeout_s: int = 90) -> str:
    payload = shlex.quote(json.dumps(body))
    return (
        f"curl -sS --max-time {timeout_s} -X POST "
        "http://localhost:11343/v3/ai/language-model "
        "-H 'Content-Type: application/json' "
        "-H 'Authorization: Bearer dummy' "
        "-H 'ai-language-model-id: freeride/coding' "
        "-H 'ai-language-model-streaming: true' "
        f"-d {payload}"
    )


@timed("fx_models_catalog")
def step_models(sandbox):
    r = sandbox.process.exec(
        "curl -fsS --max-time 30 -H 'Authorization: Bearer dummy' "
        "http://localhost:11343/coding-agent/v1/models"
    )
    if r.exit_code != 0:
        return False, "catalog request failed", (r.result or "")[-500:]
    try:
        data = json.loads(r.result)
    except ValueError:
        return False, "catalog is not JSON", (r.result or "")[-500:]
    ids = [m.get("id", "") for m in data.get("models", data.get("data", []))]
    has_coding = any("coding" in i for i in ids)
    return (
        bool(ids) and has_coding,
        f"{len(ids)} models, coding preset {'present' if has_coding else 'MISSING'}",
        ", ".join(ids[:6]),
    )


@timed("fx_pong_stream")
def step_pong(sandbox):
    r = sandbox.process.exec(_curl_fx(PONG_BODY))
    out = r.result or ""
    if r.exit_code != 0:
        return False, "request failed", out[-500:]
    text = "".join(
        json.loads(line[6:]).get("delta", "")
        for line in out.splitlines()
        if line.startswith("data: ") and '"text-delta"' in line
    )
    finished = '"type":"finish"' in out.replace(" ", "")
    got_pong = "pong" in text.lower()
    return (
        bool(text) and finished,
        f"text={text.strip()[:40]!r} finish={finished} pong={got_pong}",
        out[-500:],
    )


@timed("fx_tool_roundtrip")
def step_tool_call(sandbox):
    r = sandbox.process.exec(_curl_fx(WRITE_TOOL_BODY, timeout_s=120))
    out = r.result or ""
    if r.exit_code != 0:
        return False, "request failed", out[-500:]
    tool_calls = [
        json.loads(line[6:])
        for line in out.splitlines()
        if line.startswith("data: ") and '"tool-call"' in line
    ]
    if not tool_calls:
        finish = "finish reached" if '"type":"finish"' in out.replace(" ", "") else "no finish"
        return False, f"no tool-call event ({finish})", out[-500:]
    tc = tool_calls[0]
    inp = tc.get("input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return False, "tool-call input is unparseable JSON", out[-500:]
    ok = (
        bool(tc.get("toolCallId"))
        and tc.get("toolName") == "Write"
        and isinstance(inp, dict)
        and "hi" in str(inp.get("content", "")).lower()
    )
    return ok, f"toolName={tc.get('toolName')} input_keys={sorted(inp) if isinstance(inp, dict) else '?'}", out[-500:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="cli")
    ap.add_argument("--env-file", default=None)
    args = ap.parse_args()

    report = PhaseReport(phase=f"fx-agent (ref={args.ref})")
    with ephemeral_sandbox("freeride-fx-agent") as (sandbox, dt):
        report.sandbox_id = sandbox.id
        report.sandbox_create_s = dt

        for step in (
            step_install_uv(sandbox),
            step_install_freeride(sandbox, args.ref),
            step_upload_env(sandbox, args.env_file),
            step_launch_gateway(sandbox),
            step_wait_for_health(sandbox),
        ):
            report.add(step)
            if not step.passed:
                print(report.summary(verbose=True))
                return 1

        report.add(step_models(sandbox))
        report.add(step_pong(sandbox))
        report.add(step_tool_call(sandbox))

    print(report.summary(verbose=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
