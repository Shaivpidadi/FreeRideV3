"""Provider-agnostic error classification.

The Provider Protocol's ``classify_error`` returns one of these. The
resolver and retry loop branch on the kind, not on raw HTTP responses.
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    """Coarse classification of a provider response failure.

    Stays small on purpose — the retry policy is the only consumer that
    needs to fan out, and it cares about ~3 buckets.

    ``OK`` is included so providers can return ``ErrorKind.OK`` from
    ``classify_error`` for non-failure responses without a separate
    sentinel.
    """

    OK = "ok"
    RATE_LIMIT = "rate_limit"  # 429-style; this key is cooling down, try another
    QUOTA_EXHAUSTED = "quota_exhausted"  # this key is dead until tomorrow / next cycle
    MODEL_NOT_FOUND = "model_not_found"  # try another model on the same provider
    UNAVAILABLE = "unavailable"  # provider 5xx; transient
    TIMEOUT = "timeout"
    AUTH = "auth"  # invalid key (NIM uses HTTP 403 for this; OpenRouter uses 401)
    UNKNOWN = "unknown"


# Errors that may clear if the same (provider, model, key) tuple is retried
# after a brief backoff. Everything else (AUTH, MODEL_NOT_FOUND, QUOTA_EXHAUSTED)
# requires the resolver to advance to a different tuple — that's the retry
# loop's job in Phase 2, not this predicate's.
_RETRYABLE_SAME_TUPLE: frozenset[ErrorKind] = frozenset(
    {ErrorKind.RATE_LIMIT, ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT}
)


def is_retryable(kind: ErrorKind) -> bool:
    """True if the same (provider, model, key) tuple is worth retrying after
    a backoff. Narrow on purpose: ``RATE_LIMIT`` (the limit may reset),
    ``UNAVAILABLE`` (provider 5xx, transient), ``TIMEOUT`` (network blip).

    For the broader "should the resolver try a different tuple?" decision,
    the retry loop in Phase 2 fans out per :class:`ErrorKind` directly —
    AUTH and QUOTA_EXHAUSTED advance the key; MODEL_NOT_FOUND advances the
    model; UNKNOWN surfaces to the client to avoid doubling the bill on
    something we don't understand.
    """
    return kind in _RETRYABLE_SAME_TUPLE
