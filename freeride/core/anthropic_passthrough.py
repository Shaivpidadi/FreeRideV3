"""Transparent passthrough to ``api.anthropic.com/v1/messages``.

When a Claude Code session points ``ANTHROPIC_BASE_URL`` at FreeRide
and sends a ``claude-*`` model id with auth (OAuth bearer from their
subscription, or a raw API key), we relay the request to Anthropic
unchanged. FreeRide is invisible in that path.

Design constraints:

- **Raw bytes in, raw bytes out.** We don't parse the body, don't
  re-serialize, don't translate. Mutating the JSON risks dropping
  fields our schema hasn't caught up to (``cache_control``,
  ``thinking``, beta-gated headers). The caller's request is shipped
  as-is.
- **Tokens are radioactive.** Auth headers are never logged. Telemetry
  records presence only (``auth_present=true``) and a short
  non-reversible prefix hash for incident triage.
- **Headers forwarded:** anthropic-version, anthropic-beta,
  content-type, the auth header (Authorization or x-api-key).
  Stripped: hop-by-hop headers, Host (httpx sets it), Content-Length
  (httpx recomputes), and any X-FreeRide-* internal markers.
- **Streaming:** if the inbound request has ``stream: true`` in its
  JSON, we open a streaming response from Anthropic and pipe bytes
  through. We don't translate the SSE — the wire format is already
  Anthropic's, so the client sees what it would see hitting
  ``api.anthropic.com`` directly.

Telemetry events emitted (``passthrough_*``) carry enough to debug
without ever capturing a credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from freeride.core.events import emit as emit_event


logger = logging.getLogger(__name__)


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Headers we copy from the inbound request when present. Anything not
# on this list is dropped — keeps the relayed request minimal and
# avoids forwarding internal FreeRide markers or hop-by-hop headers
# that httpx will set itself.
_FORWARDED_REQUEST_HEADERS = {
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "content-type",
    "user-agent",
}

# Auth headers we relay. Either one (not both) — Claude Code's OAuth
# flow uses Authorization; raw-key flow uses x-api-key.
_AUTH_HEADERS = ("authorization", "x-api-key")

# Per-request timeouts. Anthropic's own SDK uses 10-minute default
# read timeouts on streaming responses; we match that. Connect timeout
# is short — if we can't reach api.anthropic.com in 10s, we want to
# fail fast and let the caller see it.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)


# ─── helpers ────────────────────────────────────────────────────────


def _auth_fingerprint(token: str) -> str:
    """A short non-reversible identifier for an auth token, safe for
    telemetry and logs. SHA-256 of the token, first 8 hex chars.

    This is enough to tell two requests "used the same credential"
    (useful when debugging quota or rate-limit issues) without ever
    storing the credential itself. Eight hex chars = 32 bits of
    discrimination — collisions are possible but the value is only
    used as a *hint* in telemetry, not as an identity.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()[:8]


def _select_forwarded_headers(inbound: dict[str, str]) -> dict[str, str]:
    """Build the header dict for the outbound request to Anthropic.

    Inbound header keys arrive lowercased from Starlette/httpx; we
    preserve that convention on the way out so httpx normalizes
    consistently.
    """
    out: dict[str, str] = {}
    for k, v in inbound.items():
        kl = k.lower()
        if not v:
            continue
        if kl in _FORWARDED_REQUEST_HEADERS or kl in _AUTH_HEADERS:
            out[kl] = v
    return out


def _select_response_headers(upstream: httpx.Headers) -> dict[str, str]:
    """Headers to relay back to the caller from Anthropic's response.

    Drop hop-by-hop headers, Transfer-Encoding (httpx/Starlette handle
    chunking), and Content-Length (Starlette recomputes for buffered
    responses). Keep request-id and rate-limit headers — those are
    useful to the client.
    """
    skip = {
        "transfer-encoding",
        "content-encoding",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
    out: dict[str, str] = {}
    for k, v in upstream.items():
        if k.lower() in skip:
            continue
        out[k] = v
    return out


def _peek_streaming(body_bytes: bytes) -> bool:
    """Cheap JSON-peek to detect ``"stream": true`` without a full
    Pydantic parse. We don't want to validate the body here — the
    whole point of passthrough is to NOT touch the schema. If the JSON
    is malformed Anthropic will reject it; we shouldn't pre-empt that
    decision.
    """
    try:
        parsed = json.loads(body_bytes)
    except (ValueError, TypeError):
        return False
    return bool(parsed.get("stream"))


# ─── main entry: passthrough one request ────────────────────────────


async def relay_to_anthropic(
    *,
    body_bytes: bytes,
    inbound_headers: dict[str, str],
    request_id: str,
    model_id: str,
) -> Response:
    """Relay a raw ``/v1/messages`` request to api.anthropic.com.

    Parameters
    ----------
    body_bytes
        The raw request body as received from the caller. Forwarded
        verbatim to Anthropic.
    inbound_headers
        Lowercased headers from the inbound request. Auth and a small
        allowlist are forwarded; everything else is dropped.
    request_id
        The FreeRide request-id stamped on telemetry. Surfaced back
        to the caller as ``X-FreeRide-Request-Id`` so they can
        correlate.
    model_id
        Echoed in telemetry. Not used for routing — that decision
        already happened upstream.

    Returns either a ``JSONResponse`` (non-streaming) or a
    ``StreamingResponse`` (when the body has ``stream: true``).
    """
    forward_headers = _select_forwarded_headers(inbound_headers)

    # Token fingerprint for telemetry. Picks whichever auth flavor
    # was used — preference order matters only insofar as we don't
    # double-stamp the event.
    token_value = ""
    auth_kind = "none"
    for h in _AUTH_HEADERS:
        v = inbound_headers.get(h, "")
        if v:
            token_value = v
            auth_kind = h
            break
    fp = _auth_fingerprint(token_value)

    streaming = _peek_streaming(body_bytes)

    emit_event(
        "passthrough_start",
        request_id=request_id,
        model=model_id,
        streaming=streaming,
        auth_kind=auth_kind,
        auth_fingerprint=fp,
        endpoint="messages",
    )

    if streaming:
        return await _stream_passthrough(
            body_bytes=body_bytes,
            forward_headers=forward_headers,
            request_id=request_id,
            model_id=model_id,
        )
    return await _buffered_passthrough(
        body_bytes=body_bytes,
        forward_headers=forward_headers,
        request_id=request_id,
        model_id=model_id,
    )


# ─── non-streaming path ─────────────────────────────────────────────


async def _buffered_passthrough(
    *,
    body_bytes: bytes,
    forward_headers: dict[str, str],
    request_id: str,
    model_id: str,
) -> Response:
    """Single round-trip. Buffer Anthropic's response, mirror status +
    headers + body back to the caller."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                content=body_bytes,
                headers=forward_headers,
            )
    except httpx.HTTPError as e:
        emit_event(
            "passthrough_transport_error",
            request_id=request_id,
            model=model_id,
            error=type(e).__name__,
            endpoint="messages",
        )
        raise HTTPException(
            status_code=502,
            detail={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        f"FreeRide could not reach api.anthropic.com: "
                        f"{type(e).__name__}. The gateway is configured "
                        "to passthrough but the upstream is unreachable."
                    ),
                    "request_id": request_id,
                },
            },
        ) from e

    emit_event(
        "passthrough_response",
        request_id=request_id,
        model=model_id,
        status=resp.status_code,
        endpoint="messages",
    )

    response_headers = _select_response_headers(resp.headers)
    response_headers["X-FreeRide-Provider"] = "anthropic-passthrough"
    response_headers["X-FreeRide-Request-Id"] = request_id

    # Return the raw body — don't re-encode. Anthropic's content-type
    # is preserved through _select_response_headers.
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ─── streaming path ─────────────────────────────────────────────────


async def _stream_passthrough(
    *,
    body_bytes: bytes,
    forward_headers: dict[str, str],
    request_id: str,
    model_id: str,
) -> StreamingResponse:
    """Open a streaming response from Anthropic and pipe bytes through.

    The wire format is already Anthropic SSE; we don't touch it. Any
    mid-stream upstream error becomes a truncated stream from the
    client's perspective — same constraint as the free-route's
    streaming failover (rare in practice; logged).

    We open the httpx client inside the generator so it stays alive
    for the lifetime of the stream. ``aclose()`` runs when the
    generator exits (normally or via exception), closing the
    connection.
    """
    async def byte_stream() -> AsyncIterator[bytes]:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    ANTHROPIC_API_URL,
                    content=body_bytes,
                    headers=forward_headers,
                ) as resp:
                    emit_event(
                        "passthrough_response",
                        request_id=request_id,
                        model=model_id,
                        status=resp.status_code,
                        streaming=True,
                        endpoint="messages",
                    )
                    if resp.status_code >= 400:
                        # Anthropic rejected before streaming started.
                        # Read the body so the caller sees the error.
                        # JSON or SSE both come through as bytes here.
                        body = await resp.aread()
                        yield body
                        return
                    # Use aiter_bytes (NOT aiter_raw) so httpx
                    # transparently decompresses gzip/deflate before
                    # we forward. We strip content-encoding from the
                    # response headers (it's in the skip set) so the
                    # client must receive plain bytes for the
                    # framing to match.
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            emit_event(
                "passthrough_transport_error",
                request_id=request_id,
                model=model_id,
                error=type(e).__name__,
                streaming=True,
                endpoint="messages",
            )
            # Best-effort error envelope. By the time we land here the
            # connection MAY have shipped some bytes; we can only
            # append, not retract.
            err = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        f"FreeRide passthrough lost the upstream stream: "
                        f"{type(e).__name__}."
                    ),
                },
            }
            yield f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8")

    return StreamingResponse(
        byte_stream(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": "anthropic-passthrough",
            "X-FreeRide-Request-Id": request_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


__all__ = [
    "relay_to_anthropic",
    "ANTHROPIC_API_URL",
]
