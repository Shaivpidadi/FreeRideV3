"""Tests for `freeride bench`.

The network-side (httpx round-trip to the gateway) is exercised in the
e2e suite. Here we cover:
  - _percentile() correctness
  - _format_table() column alignment + sort by p50 + failed-row dimming
  - _bench_one() success and partial-failure paths via httpx_mock
"""

from __future__ import annotations

import re

import pytest

from freeride.cli.cmd_bench import (
    _bench_one,
    _format_table,
    _percentile,
)


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50) == 0.0

    def test_single(self):
        assert _percentile([100.0], 50) == 100.0
        assert _percentile([100.0], 95) == 100.0

    def test_p50_three_samples(self):
        assert _percentile([100.0, 200.0, 300.0], 50) == 200.0

    def test_p95_interpolated(self):
        # With 5 samples, p95 falls between the 4th and 5th element.
        assert _percentile([100.0, 200.0, 300.0, 400.0, 500.0], 95) == pytest.approx(480.0)


# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_sorts_by_p50_ascending(self):
        rows = [
            {"provider": "slow", "ok": 3, "n": 3, "p50_ms": 800, "p95_ms": 900, "tok_per_s": 30, "failures": []},
            {"provider": "fast", "ok": 3, "n": 3, "p50_ms": 100, "p95_ms": 150, "tok_per_s": 200, "failures": []},
            {"provider": "mid", "ok": 3, "n": 3, "p50_ms": 400, "p95_ms": 500, "tok_per_s": 80, "failures": []},
        ]
        out = _strip_ansi(_format_table(rows, no_color=True))
        # Order in output (after header + separator rows) should be fast, mid, slow.
        body_lines = [line for line in out.split("\n") if "ms" in line and "p" not in line.lower()]
        assert body_lines[0].startswith("fast")
        assert body_lines[1].startswith("mid")
        assert body_lines[2].startswith("slow")

    def test_failed_rows_pushed_to_bottom(self):
        rows = [
            {"provider": "ok-prov", "ok": 3, "n": 3, "p50_ms": 100, "p95_ms": 200, "tok_per_s": 50, "failures": []},
            {"provider": "failing", "ok": 0, "n": 3, "p50_ms": None, "p95_ms": None, "tok_per_s": None, "failures": ["http 401"]},
        ]
        out = _strip_ansi(_format_table(rows, no_color=True))
        ok_idx = out.index("ok-prov")
        fail_idx = out.index("failing")
        assert ok_idx < fail_idx, "failed rows should sort after ok rows"

    def test_winner_line_appended(self):
        rows = [
            {"provider": "groq", "ok": 3, "n": 3, "p50_ms": 142, "p95_ms": 287, "tok_per_s": 98, "failures": []},
        ]
        out = _strip_ansi(_format_table(rows, no_color=True))
        assert "Fastest: groq" in out
        assert "142ms" in out

    def test_failed_summary_when_any_failures(self):
        rows = [
            {"provider": "down", "ok": 0, "n": 3, "p50_ms": None, "p95_ms": None, "tok_per_s": None, "failures": ["http 503: all_upstreams_failed"]},
        ]
        out = _strip_ansi(_format_table(rows, no_color=True))
        assert "Failed:" in out
        assert "down" in out

    def test_color_codes_off_when_requested(self):
        rows = [
            {"provider": "x", "ok": 3, "n": 3, "p50_ms": 100, "p95_ms": 200, "tok_per_s": 50, "failures": []},
        ]
        assert "\x1b[" not in _format_table(rows, no_color=True)


# ---------------------------------------------------------------------------


class TestBenchOne:
    def test_all_succeed(self, httpx_mock):
        httpx_mock.add_response(
            url="http://localhost:11343/v1/chat/completions",
            method="POST",
            json={
                "id": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
            is_reusable=True,
        )
        result = _bench_one(
            gateway_url="http://localhost:11343/v1",
            provider="openrouter",
            model="openrouter/free",
            prompt="hi",
            n=3,
        )
        assert result["ok"] == 3
        assert result["n"] == 3
        assert result["p50_ms"] is not None
        assert result["p95_ms"] is not None
        # 7 tokens × 3 calls / total time → some non-None number.
        assert result["tok_per_s"] is not None
        assert result["failures"] == []

    def test_partial_failure_records_reasons(self, httpx_mock):
        # First call fails with 401; second + third succeed.
        httpx_mock.add_response(
            url="http://localhost:11343/v1/chat/completions",
            method="POST",
            status_code=401,
            json={"error": {"type": "invalid_api_key"}},
        )
        httpx_mock.add_response(
            url="http://localhost:11343/v1/chat/completions",
            method="POST",
            json={
                "id": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            },
            is_reusable=True,
        )
        result = _bench_one(
            gateway_url="http://localhost:11343/v1",
            provider="openrouter",
            model="openrouter/free",
            prompt="hi",
            n=3,
        )
        assert result["ok"] == 2
        assert result["n"] == 3
        assert len(result["failures"]) == 1
        assert "401" in result["failures"][0]
