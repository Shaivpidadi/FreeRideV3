"""Tests for `freeride init`.

The wizard reads env-var values from input() and writes a dotenv file.
We inject a fake input() so tests are deterministic.
"""

from __future__ import annotations

import argparse
from pathlib import Path


from freeride.cli.cmd_init import (
    _DEFAULT_OUT,
    _PROVIDER_PROMPTS,
    _read_existing,
    _write_env,
    cmd_init,
)


def _scripted_input(answers: list[str]):
    """Returns an input() replacement that yields the next scripted answer
    on each call. Raises StopIteration when exhausted (caller's problem).
    """
    it = iter(answers)

    def _input(_prompt: str) -> str:
        return next(it)

    return _input


def _args(out: Path | None = None, open_browser: bool = False) -> argparse.Namespace:
    return argparse.Namespace(out=str(out) if out else None, open_browser=open_browser)


# How many input() calls a full wizard run makes — one per env var across
# all providers (CF has 2, others have 1).
_TOTAL_PROMPTS = sum(len(env_vars) for _, env_vars, _, _ in _PROVIDER_PROMPTS)


# ---------------------------------------------------------------------------


class TestExistingFileRoundTrip:
    def test_read_missing_returns_empty(self, tmp_path):
        assert _read_existing(tmp_path / "nope") == {}

    def test_read_skips_blanks_and_comments(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# comment\n\nFOO=bar\nBAZ = qux\n", encoding="utf-8")
        assert _read_existing(p) == {"FOO": "bar", "BAZ": "qux"}

    def test_write_then_read_round_trips(self, tmp_path):
        p = tmp_path / ".env"
        _write_env(p, {"OPENROUTER_API_KEY": "sk-or-test", "GROQ_API_KEY": "gsk_x"})
        back = _read_existing(p)
        assert back == {"OPENROUTER_API_KEY": "sk-or-test", "GROQ_API_KEY": "gsk_x"}


class TestWizard:
    def test_only_or_key_entered_writes_only_or(self, tmp_path):
        # Skip every provider except OpenRouter.
        answers = ["sk-or-v1-test"] + [""] * (_TOTAL_PROMPTS - 1)
        out_path = tmp_path / ".env"
        rc = cmd_init(_args(out=out_path), _input=_scripted_input(answers))
        assert rc == 0
        kvs = _read_existing(out_path)
        assert kvs == {"OPENROUTER_API_KEY": "sk-or-v1-test"}

    def test_skipping_everything_writes_nothing(self, tmp_path):
        answers = [""] * _TOTAL_PROMPTS
        out_path = tmp_path / ".env"
        rc = cmd_init(_args(out=out_path), _input=_scripted_input(answers))
        assert rc == 0
        # Nothing was set, no existing file → no file written.
        assert not out_path.exists()

    def test_preserves_existing_keys_user_didnt_change(self, tmp_path):
        # Pre-existing file with a Groq key. User runs init, only enters
        # an OpenRouter key. The Groq key should survive.
        out_path = tmp_path / ".env"
        out_path.write_text("GROQ_API_KEY=gsk_existing\n", encoding="utf-8")
        answers = ["sk-or-v1-new"] + [""] * (_TOTAL_PROMPTS - 1)
        rc = cmd_init(_args(out=out_path), _input=_scripted_input(answers))
        assert rc == 0
        kvs = _read_existing(out_path)
        assert kvs["GROQ_API_KEY"] == "gsk_existing"
        assert kvs["OPENROUTER_API_KEY"] == "sk-or-v1-new"

    def test_cf_two_var_handling(self, tmp_path):
        # CF needs 2 vars. Walk the wizard and provide them at the right
        # prompt indices.
        answers: list[str] = []
        for provider_id, env_vars, _, _ in _PROVIDER_PROMPTS:
            for env_var in env_vars:
                if env_var == "CLOUDFLARE_API_TOKEN":
                    answers.append("cf-token-test")
                elif env_var == "CLOUDFLARE_ACCOUNT_ID":
                    answers.append("cf-account-test")
                else:
                    answers.append("")
        out_path = tmp_path / ".env"
        rc = cmd_init(_args(out=out_path), _input=_scripted_input(answers))
        assert rc == 0
        kvs = _read_existing(out_path)
        assert kvs == {
            "CLOUDFLARE_API_TOKEN": "cf-token-test",
            "CLOUDFLARE_ACCOUNT_ID": "cf-account-test",
        }

    def test_keyboard_interrupt_aborts_without_writing(self, tmp_path):
        # Simulate Ctrl-C on the very first prompt.
        def _abort(_prompt: str):
            raise KeyboardInterrupt

        out_path = tmp_path / ".env"
        rc = cmd_init(_args(out=out_path), _input=_abort)
        assert rc == 1
        assert not out_path.exists()


class TestDefaults:
    def test_default_out_is_freeride_dotenv(self):
        assert _DEFAULT_OUT == Path.home() / ".freeride" / ".env"
