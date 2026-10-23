"""Stress matrix on Daytona: drain every configured provider's keys in
parallel sandboxes, then a full-chain leg and a concurrency leg.

    python tests/ci/stress_matrix.py [--legs solo,chain,concurrency] [--max-workers 8]

Reads keys from FreeRideV3/.env and ~/.freeride/.env (the first wins per
variable). Each sandbox gets only its leg's variables. Results land in
the directory given by --out (default: ./stress_out).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
from _daytona_lib import PATH_PREFIX, default_image, ephemeral_sandbox  # noqa: E402

GIT_URL = "https://github.com/Shaivpidadi/FreeRideV3.git"
KEYLESS = "ovh,kilo,blockrun"
DRIVER = HERE / "stress_driver.py"


def load_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for f in [REPO / ".env", Path.home() / ".freeride" / ".env"]:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            v = v.strip().strip('"').strip("'")
            if k and v and k not in vals and not k.startswith("DAYTONA"):
                vals[k] = v
    return vals


def provider_vars(env: dict[str, str]) -> dict[str, dict[str, str]]:
    """provider name -> {var: value} for every configured keyed provider."""
    from freeride.core.provider_env import BUILTIN_PROVIDERS

    out: dict[str, dict[str, str]] = {}
    for spec in BUILTIN_PROVIDERS:
        if spec.keyless or spec.name == "ollama":
            continue
        mine: dict[str, str] = {}
        for base in list(spec.env_vars) + list(spec.extra_required):
            for k, v in env.items():
                if k == base or re.fullmatch(re.escape(base) + r"_\d+", k):
                    mine[k] = v
        if any(k in mine for k in spec.env_vars) and all(any(k == e for k in mine) for e in spec.extra_required):
            out[spec.name] = mine
    return out


def write_env_text(vars_: dict[str, str], extra: dict[str, str] | None = None) -> bytes:
    lines = [f"{k}={v}" for k, v in vars_.items()]
    for k, v in (extra or {}).items():
        lines.append(f"{k}={v}")
    return ("\n".join(lines) + "\n").encode()


def run_leg(name: str, env_vars: dict[str, str], driver_args: list[str], out_dir: Path, ref: str) -> dict:
    t0 = time.time()
    log: list[str] = []
    result: dict = {"leg": name}
    try:
        with ephemeral_sandbox(f"fr-stress-{name}", image=default_image()) as (sb, dt):
            log.append(f"sandbox up {dt:.0f}s")
            r = sb.process.exec("pip install -q uv 2>&1 | tail -1", timeout=600)
            r = sb.process.exec(f"{PATH_PREFIX}uv tool install --prerelease=allow git+{GIT_URL}@{ref} 2>&1 | tail -2", timeout=900)
            if r.exit_code != 0:
                raise RuntimeError(f"install failed: {(r.result or '')[-300:]}")
            sb.process.exec("mkdir -p /root/.freeride")
            sb.fs.upload_file(write_env_text(env_vars), "/root/.freeride/.env")
            sb.process.exec("chmod 600 /root/.freeride/.env")
            sb.fs.upload_file(DRIVER.read_bytes(), "/root/stress_driver.py")
            launch = r'''
import os, subprocess
env = os.environ.copy()
env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
log = open("/tmp/fr.log", "wb")
subprocess.Popen(["freeride", "serve", "--port", "11343"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                 start_new_session=True, close_fds=True, env=env)
print("launched")
'''
            sb.process.code_run(launch)
            r = sb.process.exec("for i in $(seq 1 40); do curl -sf http://127.0.0.1:11343/health && exit 0; sleep 1; done; exit 1", timeout=60)
            if r.exit_code != 0:
                raise RuntimeError("gateway did not come up: " + (sb.process.exec("tail -5 /tmp/fr.log").result or ""))
            result["health"] = json.loads(r.result.strip().splitlines()[-1])
            # Warm the catalog + model health so the first requests aren't cold.
            sb.process.exec("curl -s -m 180 http://127.0.0.1:11343/v1/models -o /root/models.json; wc -c /root/models.json", timeout=240)
            r = sb.process.exec("python3 /root/stress_driver.py " + " ".join(driver_args) + " --out /root/stress_results.json 2>&1 | tail -40", timeout=3600)
            log.append((r.result or "")[-1500:])
            rr = sb.process.exec("cat /root/stress_results.json")
            data = json.loads(rr.result)
            result.update(data["summary"])
            result["rows"] = data["rows"]
            keys = sb.process.exec(f"{PATH_PREFIX}freeride keys 2>&1 | head -30").result or ""
            result["keys_after"] = keys
            result["gateway_log_tail"] = (sb.process.exec("tail -n 40 /tmp/fr.log").result or "")[-3000:]
            models = sb.process.exec("cat /root/models.json").result or ""
            try:
                result["catalog_size"] = len(json.loads(models)["data"])
            except Exception:  # noqa: BLE001
                result["catalog_size"] = None
    except Exception as e:  # noqa: BLE001
        result["failed"] = f"{type(e).__name__}: {str(e)[:400]}"
    result["wall_s"] = int(time.time() - t0)
    result["log"] = log
    (out_dir / f"{name}.json").write_text(json.dumps(result))
    return result


def summarize(results: list[dict]) -> str:
    lines = ["| leg | requests | ok | drained | tokens in | tokens out | models | top errors | wall |", "|---|---|---|---|---|---|---|---|---|"]
    for res in sorted(results, key=lambda r: r["leg"]):
        if res.get("failed"):
            lines.append(f"| {res['leg']} | — | — | — | — | — | — | FAILED: {res['failed'][:80]} | {res.get('wall_s')}s |")
            continue
        rows = res.get("rows", [])
        ok = [r for r in rows if r.get("status") == 200]
        tin = sum(r.get("prompt_tokens") or 0 for r in ok)
        tout = sum(r.get("completion_tokens") or 0 for r in ok)
        models = {}
        for r in ok:
            models[r.get("served") or "?"] = models.get(r.get("served") or "?", 0) + 1
        errs: dict[str, int] = {}
        for r in rows:
            if r.get("status") != 200:
                key = r.get("error_type") or f"http{r.get('status')}"
                if r.get("tried"):
                    key += ":" + ",".join(f"{p}={k}" for p, k in r["tried"][:3])
                errs[key] = errs.get(key, 0) + 1
        top = "; ".join(f"{k}×{v}" for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"| {res['leg']} | {len(rows)} | {len(ok)} | {res.get('drained')} | {tin} | {tout} | {len(models)} | {top[:120]} | {res.get('wall_s')}s |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", default="solo,chain,concurrency")
    ap.add_argument("--only", default="", help="comma list of provider names for solo legs")
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--out", default="stress_out")
    a = ap.parse_args()
    env = load_env()
    os.environ.setdefault("DAYTONA_API_KEY", "")
    for f in [REPO / ".env"]:
        for line in f.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k.startswith("DAYTONA") and v:
                os.environ[k] = v.strip().strip('"')
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pv = provider_vars(env)
    legs = a.legs.split(",")
    jobs: list[tuple[str, dict, list[str]]] = []
    only = {s for s in a.only.split(",") if s}
    if "solo" in legs:
        for name, vars_ in pv.items():
            if only and name not in only:
                continue
            nkeys = sum(1 for k in vars_ if not k.endswith("ACCOUNT_ID"))
            cap = 60 * nkeys + 120
            jobs.append((f"solo-{name}", {**vars_, "OPENROUTER_API_KEY": vars_.get("OPENROUTER_API_KEY", env.get("OPENROUTER_API_KEY", ""))} if name != "openrouter" else vars_,
                         ["--leg", f"solo-{name}", "--force", name, "--max-requests", str(cap), "--max-minutes", "25", "--concurrency", "3"]))
        for name in ("ovh", "kilo", "blockrun"):
            jobs.append((f"solo-{name}", {"FREERIDE_KEYLESS": name, "OPENROUTER_API_KEY": env.get("OPENROUTER_API_KEY", "")},
                         ["--leg", f"solo-{name}", "--force", name, "--max-requests", "120", "--max-minutes", "20", "--concurrency", "2"]))
    all_vars = {k: v for vars_ in pv.values() for k, v in vars_.items()}
    all_vars["FREERIDE_KEYLESS"] = KEYLESS
    if "chain" in legs:
        jobs.append(("chain-auto", all_vars, ["--leg", "chain-auto", "--model", "auto", "--max-requests", "500", "--max-minutes", "35", "--concurrency", "4", "--drain-streak", "10"]))
        jobs.append(("chain-coding", all_vars, ["--leg", "chain-coding", "--model", "freeride/coding", "--max-requests", "300", "--max-minutes", "30", "--concurrency", "3", "--drain-streak", "10"]))
    if "concurrency" in legs:
        jobs.append(("concurrency-24", all_vars, ["--leg", "concurrency-24", "--model", "auto", "--max-requests", "400", "--max-minutes", "20", "--concurrency", "24", "--drain-streak", "24"]))
    print(f"{len(jobs)} legs: {', '.join(j[0] for j in jobs)}", flush=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=a.max_workers) as ex:
        futs = {ex.submit(run_leg, n, v, args, out_dir, a.ref): n for n, v, args in jobs}
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            ok = sum(1 for r in res.get("rows", []) if r.get("status") == 200)
            print(f"done {res['leg']}: {res.get('requests')} req, {ok} ok, drained={res.get('drained')} {res.get('failed') or ''}", flush=True)
    table = summarize(results)
    (out_dir / "REPORT.md").write_text(table + "\n")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
