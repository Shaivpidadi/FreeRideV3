"""Tests for `freeride watch` event formatter.

The tail loop is hard to test hermetically (file polling, signal
handling). The formatter is the load-bearing logic — it converts
JSONL records into the lines a user sees, and it's what determines
whether the demo is readable.
"""

from __future__ import annotations

from freeride.cli.cmd_watch import _format_event


def _strip_ansi(s: str) -> str:
    """Drop ANSI color escapes for assertion clarity."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


class TestFormatter:
    def test_request_start_streaming(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "request_start",
            "request_id": "req_abc12345",
            "model": "openrouter/free",
            "streaming": True,
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "▶ request" in out
        assert "openrouter/free" in out
        assert "stream" in out
        assert "req_abc12345" in out

    def test_provider_attempt(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "provider_attempt",
            "request_id": "req_abc12345",
            "provider": "openrouter",
            "key_index": 1,
            "model": "openrouter/free",
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "→ openrouter[k1]" in out
        assert "openrouter/free" in out

    def test_provider_response_ok(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "provider_response",
            "request_id": "req_abc12345",
            "provider": "groq",
            "key_index": 0,
            "duration_ms": 318,
            "status": "OK",
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "← groq[k0]" in out
        assert "318ms" in out
        assert "OK" in out
        assert "✓" in out

    def test_provider_response_with_retry_after(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "provider_response",
            "request_id": "req_abc12345",
            "provider": "openrouter",
            "key_index": 0,
            "duration_ms": 412,
            "status": "RATE_LIMIT",
            "retry_after_s": 47,
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "RATE_LIMIT" in out
        assert "(retry-after 47s)" in out

    def test_provider_response_first_chunk_marker(self) -> None:
        rec = {
            "type": "provider_response",
            "ts": 1712345678.0,
            "request_id": "req_x",
            "provider": "groq",
            "key_index": 0,
            "duration_ms": 100,
            "status": "OK",
            "first_chunk": True,
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "first-chunk" in out

    def test_request_complete(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "request_complete",
            "request_id": "req_abc12345",
            "provider": "groq",
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "■ complete" in out
        assert "via groq" in out

    def test_request_failed(self) -> None:
        rec = {
            "ts": 1712345678.123,
            "type": "request_failed",
            "request_id": "req_abc12345",
            "phase": "all_attempts_exhausted",
            "tried": ["openrouter", "groq"],
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        assert "✗ failed" in out
        assert "all_attempts_exhausted" in out
        assert "openrouter,groq" in out

    def test_unknown_type_falls_back_to_json(self) -> None:
        rec = {
            "ts": 1712345678.0,
            "type": "some_future_event",
            "request_id": "req_x",
            "extra": "data",
        }
        out = _strip_ansi(_format_event(rec, no_color=True))
        # Unknown types shouldn't blow up — they get stringified.
        assert "some_future_event" in out

    def test_color_codes_present_when_enabled(self) -> None:
        rec = {
            "ts": 1712345678.0,
            "type": "provider_response",
            "request_id": "req_x",
            "provider": "groq",
            "key_index": 0,
            "duration_ms": 100,
            "status": "OK",
        }
        out = _format_event(rec, no_color=False)
        # Should contain at least one ANSI escape when colors are on.
        assert "\x1b[" in out
        # Should be stripped clean when --no-color is set.
        assert "\x1b[" not in _format_event(rec, no_color=True)
