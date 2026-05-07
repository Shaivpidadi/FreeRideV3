"""Hermetic tests for freeride.core.events.

Covers: enable/disable via env, atomic append, JSONL shape,
rotation at the size cap, single-backup retention, opt-out path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freeride.core import events


@pytest.fixture
def events_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets its own tmp file, isolated from ~/.freeride/."""
    p = tmp_path / "events.jsonl"
    monkeypatch.setenv("FREERIDE_EVENTS_PATH", str(p))
    monkeypatch.delenv("FREERIDE_EVENTS", raising=False)
    return p


class TestEmit:
    def test_writes_jsonl_line(self, events_path: Path) -> None:
        events.emit("request_start", request_id="req_abc", model="x/y")
        line = events_path.read_text().strip()
        rec = json.loads(line)
        assert rec["type"] == "request_start"
        assert rec["request_id"] == "req_abc"
        assert rec["model"] == "x/y"
        assert isinstance(rec["ts"], float)

    def test_appends_multiple_events(self, events_path: Path) -> None:
        events.emit("request_start", request_id="r1")
        events.emit("provider_attempt", request_id="r1", provider="openrouter")
        events.emit("request_complete", request_id="r1")
        lines = events_path.read_text().strip().split("\n")
        assert len(lines) == 3
        types = [json.loads(line)["type"] for line in lines]
        assert types == ["request_start", "provider_attempt", "request_complete"]

    def test_disabled_via_env(self, events_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FREERIDE_EVENTS", "0")
        events.emit("request_start", request_id="r1")
        assert not events_path.exists()

    def test_disabled_accepts_various_falsy_strings(
        self, events_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ["0", "false", "no", "off", "FALSE", "Off"]:
            monkeypatch.setenv("FREERIDE_EVENTS", v)
            events.emit("request_start", request_id="x")
            assert not events_path.exists(), f"expected disabled for FREERIDE_EVENTS={v!r}"

    def test_emit_never_raises_on_io_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point at a path that can't be written (parent is a file, not dir).
        bogus = tmp_path / "not-a-dir-but-a-file"
        bogus.write_text("x")
        monkeypatch.setenv("FREERIDE_EVENTS_PATH", str(bogus / "events.jsonl"))
        # This should NOT raise — event logging is best-effort.
        events.emit("request_start", request_id="r1")

    def test_request_id_is_unique_and_short(self) -> None:
        ids = {events.new_request_id() for _ in range(50)}
        assert len(ids) == 50
        for i in ids:
            assert i.startswith("req_")
            assert len(i) == 12  # "req_" + 8 hex chars


class TestRotation:
    def test_rotates_at_size_cap(self, events_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop the size cap so we don't have to write 1MiB to test rotation.
        monkeypatch.setattr(events, "_MAX_BYTES", 200)
        # Fill past the cap.
        for i in range(50):
            events.emit("provider_attempt", request_id=f"r{i}", provider="openrouter")
        backup = events_path.with_name(events_path.name + ".1")
        assert backup.exists(), "expected rotation to create .1 backup"
        # Active file should be smaller than the backup (or at least < cap).
        assert events_path.stat().st_size < backup.stat().st_size

    def test_single_backup_only(
        self, events_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # After two rotations there should still be only one .1 backup.
        monkeypatch.setattr(events, "_MAX_BYTES", 100)
        for i in range(120):
            events.emit("provider_attempt", request_id=f"r{i}", provider="x")
        # No .2 should exist.
        assert not events_path.with_name(events_path.name + ".2").exists()
