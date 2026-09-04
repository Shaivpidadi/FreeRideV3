"""Reconcile the two telemetry Neon databases after a quota outage.

The Worker dual-writes every row to both DATABASE_URL and
DATABASE_URL_B (see src/worker.js), so in steady state the two sides
are identical. When one side is quota-dead its writes fail silently
and it develops a gap; run this once after its quota resets to copy
the missing rows across. Idempotent in both directions — every copy
is keyed on the table's natural key with ON CONFLICT DO NOTHING (or
DO UPDATE for openrouter_daily, where the newest scrape wins).

Usage:
  python3 sync_dbs.py             # URLs from .dev.vars next to this file
  python3 sync_dbs.py --dry-run   # report the gap, copy nothing
  python3 sync_dbs.py --a URL --b URL

Requires: psql on PATH (any recent client). No Python deps.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

# table -> (natural key columns, all columns, conflict action)
TABLES = {
    "beacons": (
        ("installation_id", "received_at"),
        ("installation_id", "version", "os", "tokens_served",
         "input_tokens", "output_tokens", "request_count",
         "providers_active", "uptime_hours", "received_at"),
        "DO NOTHING",
    ),
    "install_events": (
        ("installation_id",),
        ("installation_id", "version", "os", "install_method",
         "installed_at"),
        "DO NOTHING",
    ),
    "openrouter_aggregate": (
        ("fetched_at",),
        ("fetched_at", "v1_tokens", "v3_tokens", "combined_tokens"),
        "DO NOTHING",
    ),
    "openrouter_daily": (
        ("date", "app", "model_id"),
        ("date", "app", "model_id", "tokens", "scraped_at"),
        "DO UPDATE SET tokens = EXCLUDED.tokens, "
        "scraped_at = EXCLUDED.scraped_at",
    ),
}

# Applied to both sides before syncing so the beacons ON CONFLICT
# target exists on databases created before the dual-DB change.
PREPARE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_beacons_install_received "
    "ON beacons(installation_id, received_at);"
)


def psql(url: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", url, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


def urls_from_dev_vars() -> tuple[str, str]:
    text = (HERE / ".dev.vars").read_text()
    a = re.search(r"^DATABASE_URL=(.+)$", text, re.M)
    b = re.search(r"^DATABASE_URL_B=(.+)$", text, re.M)
    if not (a and b):
        sys.exit("DATABASE_URL / DATABASE_URL_B not found in .dev.vars")
    return a.group(1).strip().strip('"'), b.group(1).strip().strip('"')


def copy_missing(src: str, dst: str, src_name: str, dst_name: str,
                 dry_run: bool) -> None:
    for table, (key, cols, conflict) in TABLES.items():
        key_list = ", ".join(key)
        col_list = ", ".join(cols)
        dst_keys = psql(dst, f"SELECT {key_list} FROM {table}")
        have = set(dst_keys.splitlines())
        src_rows = psql(src, f"SELECT {key_list} FROM {table}")
        missing = [r for r in src_rows.splitlines() if r not in have]
        print(f"  {table}: {src_name}->{dst_name} missing {len(missing)}")
        if dry_run or not missing:
            continue
        # Stage the whole source table through COPY and let ON CONFLICT
        # drop the rows the target already has. The temp table is
        # created via AS SELECT ... WITH NO DATA so it carries column
        # types but no sequences/defaults. One psql session, data
        # inline, so COPY and the INSERT share the temp table.
        dump = subprocess.run(
            ["psql", src, "-v", "ON_ERROR_STOP=1",
             "-c", f"\\copy {table} ({col_list}) TO STDOUT"],
            capture_output=True, text=True,
        )
        if dump.returncode != 0:
            raise RuntimeError(f"{table} dump: {dump.stderr.strip()}")
        script = (
            # Neon's pooler reuses a backend across our per-table psql
            # sessions, so a temp table from the previous table can still
            # be attached. Drop it first rather than assume a clean session.
            "DROP TABLE IF EXISTS _sync;\n"
            f"CREATE TEMP TABLE _sync AS "
            f"SELECT {col_list} FROM {table} WITH NO DATA;\n"
            f"COPY _sync ({col_list}) FROM STDIN;\n"
            f"{dump.stdout}\\.\n"
            f"INSERT INTO {table} ({col_list}) "
            f"SELECT {col_list} FROM _sync "
            f"ON CONFLICT ({key_list}) {conflict};\n"
        )
        load = subprocess.run(
            ["psql", dst, "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=script, capture_output=True, text=True,
        )
        if load.returncode != 0:
            raise RuntimeError(f"{table} load: {load.stderr.strip()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", help="first DB URL (default: .dev.vars DATABASE_URL)")
    ap.add_argument("--b", help="second DB URL (default: .dev.vars DATABASE_URL_B)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.a and args.b:
        url_a, url_b = args.a, args.b
    else:
        url_a, url_b = urls_from_dev_vars()

    for name, url in (("A", url_a), ("B", url_b)):
        alive = psql(url, "SELECT 1")
        assert alive == "1"
        psql(url, PREPARE_SQL)
        print(f"DB {name}: reachable, unique index ensured")

    print("A -> B:")
    copy_missing(url_a, url_b, "A", "B", args.dry_run)
    print("B -> A:")
    copy_missing(url_b, url_a, "B", "A", args.dry_run)
    print("done" + (" (dry run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
