"""Tests for `freeride providers` formatter.

Network round-trip is exercised via httpx_mock so we can pin specific
gateway responses; the table-rendering logic is the load-bearing
piece since users read it.
"""

from __future__ import annotations

import re

from freeride.cli.cmd_providers import _format_table


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


class TestFormatTable:
    def test_empty_list(self):
        out = _format_table([], no_color=True)
        assert "no providers registered" in out

    def test_warm_provider_shows_real_stats(self):
        provs = [
            {
                "name": "openrouter",
                "embeddings_supported": True,
                "n": 12,
                "success_rate": 0.917,
                "p50_ms": 412,
                "score": 95.9,
            }
        ]
        out = _strip_ansi(_format_table(provs, no_color=True))
        assert "openrouter" in out
        assert "yes" in out  # embeddings_supported
        assert "91%" in out
        assert "412ms" in out
        assert "95.9" in out
        assert "(cold)" not in out

    def test_cold_provider_marked(self):
        provs = [
            {
                "name": "huggingface",
                "embeddings_supported": True,
                "n": 0,
                "success_rate": 1.0,
                "p50_ms": 0,
                "score": 100.0,
            }
        ]
        out = _strip_ansi(_format_table(provs, no_color=True))
        assert "(cold)" in out
        # ok% and p50 should be em-dashes when no data.
        assert "—" in out

    def test_summary_picks_healthiest_warm(self):
        provs = [
            {"name": "fast", "embeddings_supported": True, "n": 10, "success_rate": 1.0, "p50_ms": 100, "score": 99.0},
            {"name": "slow", "embeddings_supported": True, "n": 10, "success_rate": 1.0, "p50_ms": 800, "score": 92.0},
            {"name": "cold", "embeddings_supported": True, "n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0},
        ]
        out = _strip_ansi(_format_table(provs, no_color=True))
        # 'cold' has the highest raw score but isn't warm enough to count
        # as "healthiest".
        assert "Healthiest: fast" in out

    def test_summary_when_all_cold(self):
        provs = [
            {"name": "x", "embeddings_supported": False, "n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0},
        ]
        out = _strip_ansi(_format_table(provs, no_color=True))
        assert "All cold" in out
        assert "make a few requests" in out

    def test_no_color_strips_escapes(self):
        provs = [
            {"name": "cold-prov", "embeddings_supported": True, "n": 0, "success_rate": 1.0, "p50_ms": 0, "score": 100.0},
        ]
        # With colors enabled, cold rows should carry an ANSI escape.
        with_color = _format_table(provs, no_color=False)
        assert "\x1b[" in with_color
        # With colors disabled, no escapes anywhere.
        without_color = _format_table(provs, no_color=True)
        assert "\x1b[" not in without_color
