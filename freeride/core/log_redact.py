"""Keep credentials out of log lines.

Provider errors are logged with their message, and some libraries put
the offending header into that message (httpx: ``Illegal header value
b'Bearer sk-...'``). A malformed key from the environment can therefore
reach the log, and the daemon log is not always private. This filter
rewrites every record before any handler formats it.
"""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    # Known key shapes.
    re.compile(r"sk-or-v1-[A-Za-z0-9]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{12,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{12,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bxai-[A-Za-z0-9]{16,}"),
    # Any bearer credential, whatever its shape.
    re.compile(r"(?i)(bearer\s+)[^\s'\"\\]+"),
    # KEY=value pairs that name a secret (a multi-line env blob echoed back).
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|API_TOKEN|TOKEN|SECRET)[A-Z0-9_]*\s*=\s*)[^\s'\"\\]+"),
]

REDACTED = "***"


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced."""
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pat.sub(REDACTED, out)
    return out


class RedactingFilter(logging.Filter):
    """Formats the record's message early and redacts it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never break logging over a bad format
            return True
        clean = redact(message)
        if clean != message or record.args:
            record.msg = clean
            record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach the filter to every handler of ``logger`` (root by default)
    and to the logger itself, so records are clean regardless of which
    handler a library adds later."""
    target = logger or logging.getLogger()
    marker = RedactingFilter
    if not any(isinstance(f, marker) for f in target.filters):
        target.addFilter(RedactingFilter())
    for h in target.handlers:
        if not any(isinstance(f, marker) for f in h.filters):
            h.addFilter(RedactingFilter())
