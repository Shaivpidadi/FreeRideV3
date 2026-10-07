#!/usr/bin/env python3
"""Export the Neon telemetry tables as D1-ready SQL.

Reads every row from one or two Neon projects (DATABASE_URL and the
optional DATABASE_URL_B of the old dual-DB setup), unions them by
natural key, and writes ``INSERT OR IGNORE`` batches that
``wrangler d1 execute --remote --file`` can apply. Nothing is written
to Neon; the projects stay intact as a backup.

    python3 migrate_neon_to_d1.py --out ./d1-import [--since EPOCH]

Two input modes:

* ``--from-export DIR`` (what the 2026-10-07 cutover used): DIR holds
  ``beacons_a.ndjson`` / ``beacons_b.ndjson`` and ``<table>_<a|b>.json``
  files pulled from the interim worker's token-gated
  ``GET /v1/_admin/export`` endpoint, so the Neon connection strings
  never had to leave Cloudflare.
* Direct: connection strings from the environment or from ``.dev.vars``
  (gitignored) in this directory.

``--since`` limits beacons and openrouter rows to those received/fetched
after EPOCH. Re-running is safe: every statement is INSERT OR IGNORE on
the table's natural key.

After importing, rebuild the rollups once:

    wrangler d1 execute freeride-telemetry --remote --file=./rebuild_rollups.sql
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:  # only needed for the direct mode
    psycopg = None

BATCH = 400


def load_dev_vars() -> None:
    p = Path(__file__).with_name(".dev.vars")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def q(v) -> str:
    """SQLite literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, dict)):
        v = json.dumps(v, separators=(",", ":"))
    s = str(v).replace("'", "''")
    return f"'{s}'"


def fetch(url: str, sql: str, params=()) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def write_batches(path: Path, table: str, columns: list[str], rows: list[tuple]) -> int:
    if not rows:
        path.write_text("-- no rows\n")
        return 0
    cols = ", ".join(columns)
    with path.open("w") as f:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i : i + BATCH]
            values = ",\n".join("(" + ", ".join(q(v) for v in r) + ")" for r in chunk)
            f.write(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES\n{values};\n")
    return len(rows)


BEACON_COLS = ["installation_id", "version", "os", "tokens_served", "input_tokens", "output_tokens",
               "request_count", "providers_active", "uptime_hours", "received_at"]


def from_export(src: Path, out: Path, since: int) -> int:
    """Convert dumps from the interim worker's export endpoint."""
    def load_json(name: str) -> list[dict]:
        p = src / name
        if not p.exists():
            return []
        d = json.loads(p.read_text())
        return d.get("rows", []) if d.get("ok") else []

    beacons: dict[tuple[str, int], dict] = {}
    for side in ("a", "b"):
        p = src / f"beacons_{side}.ndjson"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if int(r["received_at"]) <= since:
                continue
            beacons.setdefault((r["installation_id"], int(r["received_at"])), r)
    ordered = sorted(beacons.values(), key=lambda r: (int(r["received_at"]), r["installation_id"]))
    n_b = write_batches(out / "01_beacons.sql", "beacons", BEACON_COLS, [
        (r["installation_id"], r.get("version"), r.get("os"), int(r["tokens_served"]), int(r["input_tokens"]),
         int(r["output_tokens"]), int(r["request_count"]),
         json.dumps(r["providers_active"], separators=(",", ":")) if r.get("providers_active") is not None else None,
         int(r.get("uptime_hours") or 0), int(r["received_at"])) for r in ordered])

    agg: dict[int, dict] = {}
    for side in ("a", "b"):
        for r in load_json(f"openrouter_aggregate_{side}.json"):
            if int(r["fetched_at"]) > since:
                agg.setdefault(int(r["fetched_at"]), r)
    n_a = write_batches(out / "02_openrouter_aggregate.sql", "openrouter_aggregate",
                        ["fetched_at", "v1_tokens", "v3_tokens", "combined_tokens"],
                        [(int(r["fetched_at"]), int(r["v1_tokens"]), int(r["v3_tokens"]), int(r["combined_tokens"]))
                         for r in sorted(agg.values(), key=lambda r: int(r["fetched_at"]))])

    daily: dict[tuple[str, str, str], dict] = {}
    for side in ("a", "b"):
        for r in load_json(f"openrouter_daily_{side}.json"):
            key = (r["date"], r["app"], r["model_id"])
            if key not in daily or int(r["scraped_at"]) > int(daily[key]["scraped_at"]):
                daily[key] = r
    n_d = write_batches(out / "03_openrouter_daily.sql", "openrouter_daily",
                        ["date", "app", "model_id", "tokens", "scraped_at"],
                        [(r["date"], r["app"], r["model_id"], int(r["tokens"]), int(r["scraped_at"]))
                         for r in sorted(daily.values(), key=lambda r: (r["date"], r["app"], r["model_id"]))])

    inst: dict[str, dict] = {}
    for side in ("a", "b"):
        for r in load_json(f"install_events_{side}.json"):
            if r["installation_id"] not in inst or int(r["installed_at"]) < int(inst[r["installation_id"]]["installed_at"]):
                inst[r["installation_id"]] = r
    n_i = write_batches(out / "04_install_events.sql", "install_events",
                        ["installation_id", "version", "os", "install_method", "installed_at"],
                        [(r["installation_id"], r.get("version"), r.get("os"), r.get("install_method"), int(r["installed_at"]))
                         for r in sorted(inst.values(), key=lambda r: int(r["installed_at"]))])
    print(f"from export: beacons: {n_b}  openrouter_aggregate: {n_a}  openrouter_daily: {n_d}  install_events: {n_i}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./d1-import")
    ap.add_argument("--since", type=int, default=0, help="only beacons/aggregates after this epoch")
    ap.add_argument("--from-export", default=None, help="directory of export-endpoint dumps instead of Neon")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.from_export:
        return from_export(Path(args.from_export), out, args.since)

    load_dev_vars()
    if psycopg is None:
        print("psycopg is required for the direct mode (pip install psycopg)", file=sys.stderr)
        return 2
    urls = [u for u in (os.environ.get("DATABASE_URL"), os.environ.get("DATABASE_URL_B")) if u]
    if not urls:
        print("DATABASE_URL (and optionally DATABASE_URL_B) must be set, e.g. in .dev.vars", file=sys.stderr)
        return 2

    # beacons: union by (installation_id, received_at); A wins on ties.
    beacons: dict[tuple[str, int], tuple] = {}
    for url in urls:
        rows = fetch(
            url,
            """SELECT installation_id, version, os, tokens_served, input_tokens, output_tokens,
                      request_count, providers_active, uptime_hours, received_at
               FROM beacons WHERE received_at > %s ORDER BY received_at, id""",
            (args.since,),
        )
        for r in rows:
            beacons.setdefault((r[0], int(r[9])), r)
    ordered = sorted(beacons.values(), key=lambda r: (int(r[9]), r[0]))
    n_b = write_batches(
        out / "01_beacons.sql",
        "beacons",
        ["installation_id", "version", "os", "tokens_served", "input_tokens", "output_tokens",
         "request_count", "providers_active", "uptime_hours", "received_at"],
        [(r[0], r[1], r[2], int(r[3]), int(r[4]), int(r[5]), int(r[6]),
          json.dumps(r[7], separators=(",", ":")) if r[7] is not None else None,
          int(r[8]), int(r[9])) for r in ordered],
    )

    agg: dict[int, tuple] = {}
    for url in urls:
        for r in fetch(url, "SELECT fetched_at, v1_tokens, v3_tokens, combined_tokens FROM openrouter_aggregate WHERE fetched_at > %s", (args.since,)):
            agg.setdefault(int(r[0]), r)
    n_a = write_batches(out / "02_openrouter_aggregate.sql", "openrouter_aggregate",
                        ["fetched_at", "v1_tokens", "v3_tokens", "combined_tokens"],
                        [tuple(int(x) for x in r) for r in sorted(agg.values(), key=lambda r: int(r[0]))])

    daily: dict[tuple[str, str, str], tuple] = {}
    for url in urls:
        for r in fetch(url, "SELECT date, app, model_id, tokens, scraped_at FROM openrouter_daily WHERE scraped_at > %s", (args.since,)):
            key = (r[0], r[1], r[2])
            if key not in daily or int(r[4]) > int(daily[key][4]):
                daily[key] = r
    n_d = write_batches(out / "03_openrouter_daily.sql", "openrouter_daily",
                        ["date", "app", "model_id", "tokens", "scraped_at"],
                        [(r[0], r[1], r[2], int(r[3]), int(r[4])) for r in sorted(daily.values())])

    inst: dict[str, tuple] = {}
    for url in urls:
        for r in fetch(url, "SELECT installation_id, version, os, install_method, installed_at FROM install_events"):
            if r[0] not in inst or int(r[4]) < int(inst[r[0]][4]):
                inst[r[0]] = r
    n_i = write_batches(out / "04_install_events.sql", "install_events",
                        ["installation_id", "version", "os", "install_method", "installed_at"],
                        [(r[0], r[1], r[2], r[3], int(r[4])) for r in sorted(inst.values(), key=lambda r: int(r[4]))])

    print(f"sources: {len(urls)}  beacons: {n_b}  openrouter_aggregate: {n_a}  openrouter_daily: {n_d}  install_events: {n_i}")
    print(f"wrote {out}/01..04_*.sql — apply in order with:")
    print(f"  for f in {out}/0*.sql; do wrangler d1 execute freeride-telemetry --remote --file=$f; done")
    print("then: wrangler d1 execute freeride-telemetry --remote --file=./rebuild_rollups.sql")
    return 0


if __name__ == "__main__":
    sys.exit(main())
