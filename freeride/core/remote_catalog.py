"""The live catalog: probe results published by the maintainers, fetched
by every gateway, so a model that died this morning stops being picked
this afternoon without a release.

Artifact (``schema`` 1)::

    {
      "schema": 1,
      "generated_at": "2026-11-16T20:00:00Z",
      "generated_by": "freeride 0.4.0a31 audit-models --publish",
      "providers": {
        "<provider>": {
          "free_ids": ["id", ...],            # optional roster override for spec providers
          "broken": {"<model>": {"status": "timeout", "since": "...", "latency_ms": 30000}},
          "models": {"<model>": {"ok": true, "latency_ms": 2866, "tools": true, "context_length": 131072}}
        }
      }
    }

Consumption is deliberately narrow: ``broken`` entries are merged into
the model-health cache as ``remote_broken`` (24 h clock from ``since``),
and ``free_ids`` can widen or narrow a spec provider's roster. Nothing
about the user or their keys is sent: the fetch is a plain GET with the
gateway version in the User-Agent. ``FREERIDE_REMOTE_CATALOG=0`` turns
it off; ``FREERIDE_CATALOG_URL`` points it elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_URL = "https://api.free-ride.xyz/v1/catalog"
LOCAL_PATH = Path.home() / ".freeride" / "cache" / "remote_catalog.json"
REMOTE_BROKEN_STATUS = "remote_broken"
REMOTE_BROKEN_TTL_SEC = 24 * 3600
STALE_AFTER_SEC = 48 * 3600
REFRESH_INTERVAL_SEC = 6 * 3600
MAX_BYTES = 512 * 1024


def enabled() -> bool:
    return os.environ.get("FREERIDE_REMOTE_CATALOG", "1").strip().lower() not in ("0", "false", "off", "no")


def catalog_url() -> str:
    return os.environ.get("FREERIDE_CATALOG_URL", "").strip() or DEFAULT_CATALOG_URL


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def valid(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("schema") == 1
        and _parse_iso(doc.get("generated_at")) is not None
        and isinstance(doc.get("providers"), dict)
    )


# ─── fetch / load ─────────────────────────────────────────────────────


def fetch_remote_catalog(*, path: Path | None = None, timeout: float = 15.0) -> dict | None:
    """GET the catalog (conditional on the stored ETag) and persist it.
    Returns the catalog in effect afterwards, or None when nothing is
    available. Never raises: a missing catalog is the normal state for a
    new install."""
    if not enabled():
        return None
    from freeride import __version__

    dst = path or LOCAL_PATH
    current = _read_local(dst)
    headers = {"User-Agent": f"freeride-gateway/{__version__}"}
    if current and current.get("_etag"):
        headers["If-None-Match"] = current["_etag"]
    try:
        resp = httpx.get(catalog_url(), headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        logger.debug("remote catalog fetch failed: %s", e)
        return current.get("catalog") if current else None
    if resp.status_code == 304 and current:
        current["fetched_at"] = time.time()
        _write_local(dst, current)
        return current.get("catalog")
    if resp.status_code != 200 or len(resp.content) > MAX_BYTES:
        logger.debug("remote catalog: status %s, %d bytes", resp.status_code, len(resp.content))
        return current.get("catalog") if current else None
    try:
        doc = resp.json()
    except ValueError:
        return current.get("catalog") if current else None
    if not valid(doc):
        logger.debug("remote catalog: invalid document, ignored")
        return current.get("catalog") if current else None
    _write_local(dst, {"catalog": doc, "_etag": resp.headers.get("etag"), "fetched_at": time.time()})
    return doc


def _read_local(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) and valid(raw.get("catalog")) else None
    except (OSError, ValueError):
        return None


def _write_local(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except OSError as e:
        logger.debug("remote catalog: save failed: %s", e)


def load_remote_catalog(path: Path | None = None) -> dict:
    """The locally cached catalog, or ``{}`` when absent, disabled, or
    older than :data:`STALE_AFTER_SEC`."""
    if not enabled():
        return {}
    current = _read_local(path or LOCAL_PATH)
    if not current:
        return {}
    if time.time() - float(current.get("fetched_at") or 0) > STALE_AFTER_SEC:
        return {}
    return current.get("catalog") or {}


# ─── consumption ──────────────────────────────────────────────────────


def broken_entries(catalog: dict, *, now: float | None = None) -> dict[str, tuple[str, int, int]]:
    """``{"provider::model": (REMOTE_BROKEN_STATUS, latency_ms, since_ts)}``
    for every broken row still inside its 24 h clock."""
    out: dict[str, tuple[str, int, int]] = {}
    current = time.time() if now is None else now
    for provider, block in (catalog.get("providers") or {}).items():
        for model, info in ((block or {}).get("broken") or {}).items():
            since = _parse_iso((info or {}).get("since")) or _parse_iso(catalog.get("generated_at")) or 0
            if current - since > REMOTE_BROKEN_TTL_SEC:
                continue
            out[f"{provider}::{model}"] = (REMOTE_BROKEN_STATUS, int((info or {}).get("latency_ms") or 0), int(since))
    return out


def free_ids_override(provider: str, catalog: dict | None = None) -> frozenset[str] | None:
    """A provider's roster from the live catalog, if it carries one."""
    doc = catalog if catalog is not None else load_remote_catalog()
    block = (doc.get("providers") or {}).get(provider) or {}
    ids = block.get("free_ids")
    if isinstance(ids, list) and ids:
        return frozenset(str(i) for i in ids if isinstance(i, str) and i)
    return None


# ─── publishing ───────────────────────────────────────────────────────


# Only verdicts about the MODEL are published as broken. Quota, rate
# limit and auth verdicts describe the publisher's keys and would make
# every gateway skip models that work fine for its own user.
PUBLISHABLE_BROKEN = frozenset({"model_not_found", "timeout", "payload_too_large", "unavailable"})


def build_artifact(
    health: dict[str, Any],
    models_by_provider: dict[str, list[Any]],
    *,
    generated_by: str,
    rosters: dict[str, list[str]] | None = None,
) -> dict:
    """Turn an audit (``provider::model`` → HealthEntry) plus each
    provider's listed models into the published artifact. Key-level
    verdicts (quota, rate limit, auth) are recorded on the model row for
    transparency but never as ``broken``."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    providers: dict[str, dict] = {}
    for key, entry in health.items():
        provider, _, model = key.partition("::")
        if not provider or not model:
            continue
        block = providers.setdefault(provider, {"broken": {}, "models": {}})
        status = getattr(entry, "status", None)
        latency = int(getattr(entry, "latency_ms", 0) or 0)
        checked = int(getattr(entry, "checked_at", 0) or 0)
        since = datetime.fromtimestamp(checked, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if checked else now_iso
        if status in PUBLISHABLE_BROKEN:
            block["broken"][model] = {"status": status, "since": since, "latency_ms": latency}
        block["models"][model] = {"ok": status == "ok", "latency_ms": latency, "status": status}
    for provider, models in models_by_provider.items():
        block = providers.setdefault(provider, {"broken": {}, "models": {}})
        for m in models:
            row = block["models"].setdefault(m.api_id, {"ok": None, "latency_ms": 0})
            row["tools"] = "tools" in (m.supported_parameters or ())
            row["context_length"] = int(m.context_length or 0)
    for provider, ids in (rosters or {}).items():
        providers.setdefault(provider, {"broken": {}, "models": {}})["free_ids"] = list(ids)
    return {"schema": 1, "generated_at": now_iso, "generated_by": generated_by, "providers": providers}


def publish(artifact: dict, *, admin_url: str | None = None, token: str | None = None, timeout: float = 30.0) -> dict:
    """POST the artifact to the worker's admin endpoint. Raises on failure."""
    url = admin_url or catalog_url().replace("/v1/catalog", "/v1/_admin/catalog")
    tok = token or os.environ.get("FREERIDE_ADMIN_TOKEN", "")
    if not tok:
        raise RuntimeError("FREERIDE_ADMIN_TOKEN is not set")
    body = json.dumps(artifact).encode()
    if len(body) > MAX_BYTES:
        raise RuntimeError(f"artifact is {len(body)} bytes; limit is {MAX_BYTES}")
    resp = httpx.post(url, content=body, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
