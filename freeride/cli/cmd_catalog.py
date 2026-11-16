"""``freeride catalog`` — inspect or refresh the live catalog."""

from __future__ import annotations

import time

from freeride.core import remote_catalog


def cmd_catalog(args) -> int:
    if not remote_catalog.enabled():
        print("live catalog: disabled (FREERIDE_REMOTE_CATALOG=0)")
        return 0
    if args.action == "refresh":
        doc = remote_catalog.fetch_remote_catalog()
        if doc is None:
            print(f"live catalog: nothing available at {remote_catalog.catalog_url()}")
            return 1
    doc = remote_catalog.load_remote_catalog()
    if not doc:
        print(f"live catalog: not fetched yet (source {remote_catalog.catalog_url()}); run `freeride catalog refresh`")
        return 1
    providers = doc.get("providers") or {}
    broken = remote_catalog.broken_entries(doc)
    print(f"live catalog: generated {doc.get('generated_at')} by {doc.get('generated_by')}")
    print(f"  providers : {len(providers)}")
    print(f"  models    : {sum(len((b or {}).get('models') or {}) for b in providers.values())}")
    print(f"  broken    : {len(broken)} (within the 24 h window)")
    rosters = [p for p, b in providers.items() if (b or {}).get("free_ids")]
    if rosters:
        print(f"  rosters   : {', '.join(sorted(rosters))}")
    local = remote_catalog._read_local(remote_catalog.LOCAL_PATH) or {}
    if local.get("fetched_at"):
        print(f"  fetched   : {int((time.time() - float(local['fetched_at'])) / 60)} min ago")
    return 0
