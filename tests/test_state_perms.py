"""Security regression tests — files containing provider keys must
never be world- or group-readable.

Covers:
  - atomic_write defaults to 0o600
  - cooldown.json (keys are hashed; file mode still 0o600)
  - freeride init's .env writer also lands at 0o600
"""

from __future__ import annotations

import json
import os
import platform
import stat
from pathlib import Path

import pytest

from freeride.cli.cmd_init import _write_env
from freeride.core.cooldown import KeyCooldown
from freeride.core.state import atomic_write

_skip_on_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="POSIX permission bits don't apply to Windows ACLs",
)


def _mode_bits(p: Path) -> int:
    """Return only the rwxr-xr-x bits, dropping setuid/setgid/file-type bits."""
    return stat.S_IMODE(p.stat().st_mode)


@_skip_on_windows
class TestAtomicWriteMode:
    def test_default_is_owner_only(self, tmp_path):
        p = tmp_path / "secret.txt"
        atomic_write(p, "data")
        assert _mode_bits(p) == 0o600, f"expected 0o600, got {oct(_mode_bits(p))}"

    def test_explicit_mode_honored(self, tmp_path):
        p = tmp_path / "data.json"
        atomic_write(p, "{}", mode=0o644)
        assert _mode_bits(p) == 0o644

    def test_mode_none_skips_chmod(self, tmp_path):
        # mode=None means "leave whatever the OS default umask gave us".
        # We don't assert a specific value because umask varies, but we
        # verify it WASN'T forced down to 0o600.
        os.umask(0o022)  # standard developer-machine umask
        p = tmp_path / "open.txt"
        atomic_write(p, "x", mode=None)
        # 0o644 is the typical result; we just want != 0o600.
        assert _mode_bits(p) != 0o600 or _mode_bits(p) == 0o644

    def test_overwrites_existing_file_with_secure_mode(self, tmp_path):
        p = tmp_path / "leak.txt"
        # Pre-create world-readable.
        p.write_text("old")
        os.chmod(p, 0o644)
        atomic_write(p, "new")
        assert _mode_bits(p) == 0o600


@_skip_on_windows
class TestKeyCooldownPerms:
    def test_cooldown_file_is_owner_only(self, tmp_path):
        p = tmp_path / "cooldown.json"
        cd = KeyCooldown(path=p)
        cd.mark_rate_limited("openrouter", "sk-or-v1-secret-key")
        assert _mode_bits(p) == 0o600, (
            "cooldown.json must be 0o600 even though keys are hashed; "
            f"got {oct(_mode_bits(p))}"
        )
        body = json.loads(p.read_text())
        assert "openrouter" in body
        assert "sk-or-v1-secret-key" not in body["openrouter"]
        from freeride.core.cooldown import hash_key
        assert hash_key("sk-or-v1-secret-key") in body["openrouter"]


@_skip_on_windows
class TestInitDotenvPerms:
    def test_init_env_file_is_owner_only(self, tmp_path):
        p = tmp_path / ".env"
        _write_env(p, {"OPENROUTER_API_KEY": "sk-or-v1-secret"})
        assert _mode_bits(p) == 0o600, (
            f".env from `freeride init` contains keys; expected 0o600, "
            f"got {oct(_mode_bits(p))}"
        )
