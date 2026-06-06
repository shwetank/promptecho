"""httpx interception — the wiring that makes record/replay real.

We monkeypatch ``httpx.HTTPTransport.handle_request`` (and the async twin) so
every client built on httpx — Anthropic, OpenAI, raw httpx — routes through the
record/replay decision in :mod:`tapelog.transport`. This is the same approach
respx and vcrpy's httpx stub use. See DESIGN.md §1.

On record we read the full upstream response, capture it, and return a fresh
buffered response so the SDK can consume it normally. Streaming (SSE) responses
are captured as their ordered events and re-emitted byte-for-byte on replay.
"""

from __future__ import annotations

import base64
import json

import httpx

from .cassette import Response as Rec
from .normalizers import normalize
from .transport import decide, parse_body

# Hop-by-hop / encoding headers that won't match our re-encoded body on replay.
_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _request_body(request: httpx.Request) -> dict:
    try:
        raw = request.content
    except httpx.RequestNotRead:
        raw = request.read()
    return parse_body(raw)


def _clean_headers(headers: httpx.Headers) -> dict:
    return {k: v for k, v in dict(headers).items() if k.lower() not in _DROP_HEADERS}


def _split_sse(text: str) -> list[str]:
    """Split an SSE body into individual events (kept readable in the cassette)."""
    return [part + "\n\n" for part in text.split("\n\n") if part.strip()]


def _is_binary_content_type(ct: str) -> bool:
    """Content types we must not decode as text — images, audio, video, octet-stream.

    Anything text/* or application/json|xml|...+json round-trips cleanly through
    YAML; everything else (image/png, audio/wav, application/octet-stream, etc.)
    gets base64-encoded to preserve bytes exactly.
    """
    ct = ct.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    if ct.startswith(("image/", "audio/", "video/")):
        return True
    if ct in {"application/octet-stream", "application/pdf", "application/zip"}:
        return True
    return False


def _capture(status: int, headers: httpx.Headers, data: bytes) -> Rec:
    clean = _clean_headers(headers)
    content_type = clean.get("content-type", "")
    if "text/event-stream" in content_type:
        return Rec(status=status, headers=clean, streaming=True,
                   events=_split_sse(data.decode("utf-8", "replace")))
    if _is_binary_content_type(content_type):
        return Rec(status=status, headers=clean, streaming=False,
                   body=base64.b64encode(data).decode("ascii"), binary=True)
    try:
        body = json.loads(data) if data else None
    except ValueError:
        body = data.decode("utf-8", "replace")
    return Rec(status=status, headers=clean, streaming=False, body=body)


def _to_httpx(rec: Rec, request: httpx.Request) -> httpx.Response:
    if rec.streaming:
        content = "".join(rec.events).encode("utf-8")
    elif rec.binary and isinstance(rec.body, str):
        content = base64.b64decode(rec.body)
    elif isinstance(rec.body, (dict, list)):
        content = json.dumps(rec.body).encode("utf-8")
    elif isinstance(rec.body, str):
        content = rec.body.encode("utf-8")
    else:
        content = b""
    return httpx.Response(
        status_code=rec.status,
        headers=httpx.Headers(rec.headers),
        content=content,
        request=request,
    )


def _make_sync(cassette, mode, real_fn):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = normalize(str(request.url), _request_body(request))
        decision = decide(mode, cassette, body)
        if decision.response is not None:                      # REPLAY (no network)
            return _to_httpx(decision.response, request)
        real = real_fn(self, request)                          # PASS THROUGH
        rec = _capture(real.status_code, real.headers, real.read())
        cassette.record(request.method, str(request.url), body, rec)  # RECORD
        return _to_httpx(rec, request)

    return handle_request


def _make_async(cassette, mode, real_fn):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = normalize(str(request.url), _request_body(request))
        decision = decide(mode, cassette, body)
        if decision.response is not None:
            return _to_httpx(decision.response, request)
        real = await real_fn(self, request)
        rec = _capture(real.status_code, real.headers, await real.aread())
        cassette.record(request.method, str(request.url), body, rec)
        return _to_httpx(rec, request)

    return handle_async_request


def install(cassette, mode):
    """Patch httpx; returns a token to pass back to :func:`uninstall`."""
    saved = (httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request)
    httpx.HTTPTransport.handle_request = _make_sync(cassette, mode, saved[0])
    httpx.AsyncHTTPTransport.handle_async_request = _make_async(cassette, mode, saved[1])
    return saved


def uninstall(saved) -> None:
    httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request = saved
