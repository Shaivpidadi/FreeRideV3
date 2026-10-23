"""In-sandbox load driver for the stress matrix.

Fires chat requests at the local gateway until the leg's keys are drained
(or a cap is hit), recording one JSON row per request: provider that
served it, model, token usage, latency, error kind. Runs with only the
standard library so it needs nothing installed in the sandbox.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request

GW = "http://127.0.0.1:11343"
TOPICS = [
    "how TCP congestion control works", "why Rust's borrow checker exists", "how a B-tree index speeds up queries",
    "what makes a good retry policy for HTTP clients", "how DNS resolution works end to end", "the tradeoffs of microservices",
    "how garbage collectors decide what to free", "what a write-ahead log is for", "how OAuth 2.0 authorization code flow works",
    "why floating point math is inexact", "how a CPU cache hierarchy works", "what consistent hashing solves",
    "how TLS handshakes establish a session key", "how Git stores commits internally", "what idempotency means for APIs",
    "how LLM tokenizers split text", "why timezones are hard in software", "how a rate limiter with token buckets works",
]
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Get the weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
DRAIN_TYPES = {"no_usable_keys", "all_upstreams_failed", "no_model_available", "no_providers"}


def build(i: int, model: str) -> tuple[dict, str]:
    topic = TOPICS[i % len(TOPICS)]
    if i % 4 == 3:
        body = {"model": model, "max_tokens": 200, "tools": TOOLS, "tool_choice": "auto",
                "messages": [{"role": "user", "content": f"What is the weather in {random.choice(['Paris','Oslo','Lima','Osaka','Cairo'])}? Use the tool."}]}
        return body, "tools"
    stream = i % 6 == 5
    body = {"model": model, "max_tokens": 500, "stream": stream,
            "messages": [{"role": "user", "content": f"Explain {topic} in about 300 words, for a working engineer. Request {i}."}]}
    return body, ("stream" if stream else "chat")


def one(i: int, model: str, force: str | None, timeout: int) -> dict:
    body, kind = build(i, model)
    headers = {"Content-Type": "application/json"}
    if force:
        headers["X-FreeRide-Force-Provider"] = force
    req = urllib.request.Request(GW + "/v1/chat/completions", data=json.dumps(body).encode(), headers=headers)
    row = {"i": i, "kind": kind, "ts": time.time(), "requested": model}
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            row["status"] = resp.status
            row["provider"] = resp.headers.get("X-FreeRide-Provider")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["provider"] = e.headers.get("X-FreeRide-Provider")
        raw = e.read()
    except Exception as e:  # noqa: BLE001
        row["status"] = 0
        row["error_type"] = type(e).__name__
        row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return row
    row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    text = raw.decode("utf-8", "replace")
    if kind == "stream" and row["status"] == 200:
        n = 0
        usage = None
        served = None
        content = 0
        for line in text.split("\n"):
            if not line.startswith("data:") or "[DONE]" in line:
                continue
            n += 1
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            served = served or obj.get("model")
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    content += len(ch["delta"]["content"])
        row.update(events=n, served=served, content_chars=content)
        if usage:
            row.update(prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        return row
    try:
        obj = json.loads(text)
    except ValueError:
        row["error_type"] = "non_json"
        row["snippet"] = text[:120]
        return row
    if row["status"] == 200:
        row["served"] = obj.get("model")
        row["provider"] = row.get("provider") or obj.get("_freeride_provider")
        u = obj.get("usage") or {}
        row["prompt_tokens"] = u.get("prompt_tokens")
        row["completion_tokens"] = u.get("completion_tokens")
        msg = (obj.get("choices") or [{}])[0].get("message") or {}
        row["tool_calls"] = bool(msg.get("tool_calls"))
        row["content_chars"] = len(msg.get("content") or "")
    else:
        err = obj.get("error") or (obj.get("detail") or {}).get("error") or {}
        if isinstance(err, dict):
            row["error_type"] = err.get("type")
            row["tried"] = [(t.get("provider"), t.get("last_error")) for t in err.get("tried") or []]
            row["error_message"] = str(err.get("message", ""))[:160]
        else:
            row["error_type"] = "unknown"
            row["snippet"] = text[:120]
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", required=True)
    ap.add_argument("--force", default=None)
    ap.add_argument("--model", default="auto")
    ap.add_argument("--max-requests", type=int, default=300)
    ap.add_argument("--max-minutes", type=float, default=20)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--drain-streak", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default="/root/stress_results.json")
    a = ap.parse_args()

    rows: list[dict] = []
    lock = threading.Lock()
    stop = threading.Event()
    deadline = time.time() + a.max_minutes * 60
    counter = {"i": 0, "streak": 0, "drained": False}

    def worker():
        while not stop.is_set():
            with lock:
                if counter["i"] >= a.max_requests or time.time() > deadline:
                    stop.set()
                    return
                i = counter["i"]
                counter["i"] += 1
            row = one(i, a.model, a.force, a.timeout)
            with lock:
                rows.append(row)
                drained_err = row.get("status") in (503,) and row.get("error_type") in DRAIN_TYPES
                counter["streak"] = counter["streak"] + 1 if drained_err else 0
                if counter["streak"] >= a.drain_streak:
                    counter["drained"] = True
                    stop.set()
                if len(rows) % 10 == 0:
                    ok = sum(1 for r in rows if r.get("status") == 200)
                    print(f"[{a.leg}] {len(rows)} sent, {ok} ok, streak={counter['streak']}", flush=True)
            time.sleep(0.25)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = {"leg": a.leg, "force": a.force, "model": a.model, "requests": len(rows),
               "drained": counter["drained"], "elapsed_s": int(time.time() - (rows[0]["ts"] if rows else time.time()))}
    with open(a.out, "w") as f:
        json.dump({"summary": summary, "rows": rows}, f)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
