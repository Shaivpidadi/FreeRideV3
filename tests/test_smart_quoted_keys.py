"""A key pasted with smart quotes must be repaired, and a key with a
non-ASCII character must be dropped with a clear warning instead of
reaching httpx as ``Bearer \u201csk-...`` (which raises
``'ascii' codec can't encode character '\\u201c' in position 7``)."""

from __future__ import annotations

import logging

from freeride.core.provider_env import key_problem, parse_api_keys, strip_wrapping_quotes


class TestStripWrappingQuotes:
    def test_smart_quotes(self):
        assert strip_wrapping_quotes("\u201csk-or-v1-abc\u201d") == "sk-or-v1-abc"
        assert strip_wrapping_quotes("\u2018sk-or-v1-abc\u2019") == "sk-or-v1-abc"

    def test_straight_and_mixed_quotes(self):
        assert strip_wrapping_quotes("'sk-x'") == "sk-x"
        assert strip_wrapping_quotes('"sk-x"') == "sk-x"
        assert strip_wrapping_quotes("\u201csk-x\"") == "sk-x"
        assert strip_wrapping_quotes(" \"'sk-x'\" ") == "sk-x"

    def test_leaves_clean_values_alone(self):
        assert strip_wrapping_quotes("sk-x") == "sk-x"
        assert strip_wrapping_quotes("") == ""


class TestKeyProblem:
    def test_clean(self):
        assert key_problem("sk-or-v1-abc") is None

    def test_non_ascii_names_the_character_not_the_value(self):
        msg = key_problem("sk-or\u201cv1")
        assert msg == "contains a non-ASCII character U+201C at position 5 (a smart quote from a copy-paste?)"

    def test_whitespace(self):
        assert key_problem("sk-or v1") == "contains whitespace at position 5"


class TestParseApiKeys:
    def test_smart_quoted_key_is_repaired(self):
        assert parse_api_keys("\u201csk-or-v1-abc\u201d") == ["sk-or-v1-abc"]

    def test_json_array_entries_are_repaired_too(self):
        assert parse_api_keys('["\u201ca\u201d", "b"]') == ["a", "b"]

    def test_embedded_non_ascii_is_dropped_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="freeride.core.provider_env"):
            assert parse_api_keys("sk-or-v1-ab\u201ccd") == []
        assert "non-ASCII character U+201C" in caplog.text
        assert "freeride doctor" in caplog.text
        assert "ab\u201ccd" not in caplog.text  # the value itself never hits the log


class TestDoctor:
    def test_doctor_flags_the_variable(self, monkeypatch):
        from freeride.cli.cmd_doctor import _malformed_key_vars

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-ok")
        monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-or\u201cv1-bad")
        found = _malformed_key_vars(("OPENROUTER_API_KEY",))
        assert found == [("OPENROUTER_API_KEY_2", "contains a non-ASCII character U+201C at position 5 (a smart quote from a copy-paste?)")]

    def test_doctor_accepts_a_wrapped_key(self, monkeypatch):
        from freeride.cli.cmd_doctor import _malformed_key_vars

        monkeypatch.setenv("GROQ_API_KEY", "\u201cgsk_ok\u201d")
        assert _malformed_key_vars(("GROQ_API_KEY",)) == []
