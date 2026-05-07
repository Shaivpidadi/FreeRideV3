"""Tests for freeride.core.state — atomic write semantics and JSON helpers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from freeride.core.state import atomic_write, read_json_or, write_json_atomic


@pytest.fixture
def tmpdir() -> Path:
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestAtomicWrite:
    def test_basic_write(self, tmpdir):
        p = tmpdir / "state.txt"
        atomic_write(p, "hello")
        assert p.read_text() == "hello"

    def test_creates_parent_dirs(self, tmpdir):
        p = tmpdir / "deeply" / "nested" / "state.json"
        atomic_write(p, "{}")
        assert p.exists()

    def test_overwrites_existing(self, tmpdir):
        p = tmpdir / "state.txt"
        p.write_text("old")
        atomic_write(p, "new")
        assert p.read_text() == "new"

    def test_no_leftover_tmp_after_success(self, tmpdir):
        p = tmpdir / "state.txt"
        atomic_write(p, "x")
        # No .tmp file should remain
        leftovers = list(tmpdir.rglob("*.tmp"))
        assert leftovers == []

    def test_crash_during_replace_keeps_old_file(self, tmpdir):
        """Simulate a crash: the temp file gets written but os.replace fails.
        The original file must remain readable.
        """
        p = tmpdir / "state.txt"
        p.write_text("original")

        with patch("freeride.core.state.os.replace", side_effect=RuntimeError("crash")):
            with pytest.raises(RuntimeError):
                atomic_write(p, "would-be-new")

        # Original is intact
        assert p.read_text() == "original"


class TestWriteJsonAtomic:
    def test_round_trip(self, tmpdir):
        p = tmpdir / "data.json"
        write_json_atomic(p, {"a": 1, "b": [2, 3], "c": "x"})
        assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3], "c": "x"}

    def test_indent_default_pretty(self, tmpdir):
        p = tmpdir / "data.json"
        write_json_atomic(p, {"a": 1})
        # Default indent=2 means newlines in output
        assert "\n" in p.read_text()

    def test_indent_none_compact(self, tmpdir):
        p = tmpdir / "data.json"
        write_json_atomic(p, {"a": 1, "b": 2}, indent=None)
        # No newlines for compact
        assert "\n" not in p.read_text()


class TestReadJsonOr:
    def test_existing_file(self, tmpdir):
        p = tmpdir / "data.json"
        p.write_text('{"x": 1}')
        assert read_json_or(p, None) == {"x": 1}

    def test_missing_file_returns_default(self, tmpdir):
        assert read_json_or(tmpdir / "absent.json", {"default": True}) == {"default": True}

    def test_corrupted_file_returns_default(self, tmpdir):
        p = tmpdir / "bad.json"
        p.write_text("{not valid")
        assert read_json_or(p, []) == []

    def test_empty_file_returns_default(self, tmpdir):
        p = tmpdir / "empty.json"
        p.write_text("")
        assert read_json_or(p, "fallback") == "fallback"
