"""Suite-wide isolation.

The gateway merges ``~/.freeride/cache/remote_catalog.json`` into every
``model_health.load_cache()`` call. A developer who also runs a real
gateway on the same machine would otherwise see its live catalog leak
into tests that expect an empty cache. Point the catalog at an empty
per-test path for every test; tests that want a catalog set their own.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_remote_catalog(tmp_path, monkeypatch):
    from freeride.core import remote_catalog

    monkeypatch.setattr(remote_catalog, "LOCAL_PATH", tmp_path / "remote_catalog.json")
