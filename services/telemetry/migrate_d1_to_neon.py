"""One-off: move every row from the D1 ``freeride-telemetry`` database
into Neon. Run ONCE during the 2026-05-28 cutover. Idempotent via
``ON CONFLICT DO NOTHING`` per-table so reruns are safe.

How to use:

    # 1. Export the live D1 data
    cd services/telemetry
    wrangler d1 export freeride-telemetry \
        --output=/tmp/d1_dump_remote.sql \
        --no-schema --remote

    # 2. Apply the Postgres schema to Neon (one-time)
    PGURL='postgresql://...' psql "$PGURL" -f schema.pg.sql

    # 3. Run this migration
    PGURL='postgresql://...' python3 migrate_d1_to_neon.py /tmp/d1_dump_remote.sql

Schema differences handled:
- ``providers_active`` was TEXT (JSON string) in D1 → JSONB in Postgres.
  The implicit text→jsonb cast at INSERT works because the input is
  always a valid JSON array literal.
- ``beacons.id`` was INTEGER PRIMARY KEY AUTOINCREMENT in SQLite →
  BIGSERIAL in Postgres. We insert with explicit id values, then
  ``setval()`` to advance the sequence past MAX(id).

Never commit the live PGURL — pass it through the environment at run
time. Connection string is exactly the form Neon prints in the
console; channel-binding=require is fine (psycopg2 honors it via
the libpq URL params).
"""
from __future__ import annotations

import os
import re
import sys

import psycopg2

ON_CONFLICT = {
    "beacons":              " ON CONFLICT (id) DO NOTHING",
    "openrouter_aggregate": " ON CONFLICT (fetched_at) DO NOTHING",
    "openrouter_daily":     " ON CONFLICT (date, app, model_id) DO NOTHING",
    "install_events":       " ON CONFLICT (installation_id) DO NOTHING",
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: PGURL=postgresql://... migrate_d1_to_neon.py <dump.sql>")
        return 2
    dump_path = sys.argv[1]
    pgurl = os.environ.get("PGURL")
    if not pgurl:
        print("error: PGURL env var required (Neon connection string)")
        return 2

    conn = psycopg2.connect(pgurl)
    conn.autocommit = False
    cur = conn.cursor()

    counts: dict[str, int] = {t: 0 for t in ON_CONFLICT}
    errors: list[tuple[str, str]] = []
    table_re = re.compile(r'^INSERT INTO "([a-z_]+)" .*\);$')

    with open(dump_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("PRAGMA") or "sqlite_sequence" in line:
                continue
            if not line.startswith('INSERT INTO "'):
                continue
            m = table_re.match(line)
            if not m:
                continue
            table = m.group(1)
            if table not in ON_CONFLICT:
                continue
            # Append ON CONFLICT clause before the trailing `;`. SQLite
            # dumps already use Postgres-compatible quoting for column
            # names, so no other rewrites needed.
            sql = line[:-1] + ON_CONFLICT[table] + ";"
            try:
                cur.execute(sql)
                counts[table] += 1
            except Exception as e:  # noqa: BLE001
                errors.append((table, str(e)[:120]))
                conn.rollback()
                cur = conn.cursor()

    # Advance the BIGSERIAL sequence past the highest imported id so
    # subsequent INSERTs from the live Worker pick up where the old
    # D1 autoincrement left off.
    cur.execute(
        "SELECT setval(pg_get_serial_sequence('beacons','id'), "
        "COALESCE((SELECT MAX(id) FROM beacons), 1), true)"
    )
    print("beacons.id sequence advanced to:", cur.fetchone()[0])

    conn.commit()
    print("inserted rows:", counts)
    if errors:
        print(f"errors (first 5 of {len(errors)}):", errors[:5])

    for t in ON_CONFLICT:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t}: {cur.fetchone()[0]} rows in Neon")
    conn.close()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
