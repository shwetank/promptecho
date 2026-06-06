"""The replay/record decision logic, isolated from httpx patching mechanics.

This module is pure and unit-testable: given a mode, a cassette, and a parsed
request, it decides whether to replay a recorded response or pass through to the
network and record. The actual httpx wiring (next to TODOs) lives at the bottom.
"""

from __future__ import annotations

import json
from enum import Enum

from .cassette import Cassette, Response
from .matcher import diff_fields, fingerprint


class Mode(str, Enum):
    ONCE = "once"            # record if absent, replay if present (default)
    NONE = "none"           # replay only; error on miss (CI-safe)
    NEW_EPISODES = "new_episodes"  # replay existing, record new
    ALL = "all"             # always re-record


class CassetteMiss(Exception):
    """Raised in mode=none when an incoming request has no recording."""


def parse_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}


class Decision:
    """Outcome of looking at one request: either replay `response`, or `record`."""

    def __init__(self, *, response: Response | None = None, record: bool = False):
        self.response = response
        self.record = record


def decide(mode: Mode, cassette: Cassette, body: dict) -> Decision:
    """Core branch. No I/O — caller performs the network call / persistence."""
    key = fingerprint(body, cassette.match_on)
    existing = cassette.find(key)

    if mode is Mode.ALL:
        return Decision(record=True)

    if mode is Mode.NONE:
        if existing is None:
            raise CassetteMiss(_miss_message(cassette, body))
        return Decision(response=existing.response)

    # ONCE and NEW_EPISODES: replay if we have it, otherwise record.
    if existing is not None:
        return Decision(response=existing.response)
    return Decision(record=True)


def _miss_message(cassette: Cassette, body: dict) -> str:
    nearest = cassette.interactions[-1] if cassette.interactions else None
    if nearest is None:
        return f"No recording in {cassette.path!r} and mode=none. Re-record with mode='once'."
    changed = diff_fields(body, nearest.body, cassette.match_on)
    hint = f" Nearest recording differs on: {changed}." if changed else ""
    return f"No matching recording in {cassette.path!r} (mode=none).{hint}"


# ---------------------------------------------------------------------------
# httpx wiring (sketch — see DESIGN.md §1)
# ---------------------------------------------------------------------------
# import httpx
#
# class TapedeckTransport(httpx.BaseTransport):
#     def __init__(self, real: httpx.BaseTransport, cassette: Cassette, mode: Mode):
#         self._real, self._cassette, self._mode = real, cassette, mode
#
#     def handle_request(self, request: httpx.Request) -> httpx.Response:
#         body = parse_body(request.content)
#         decision = decide(self._mode, self._cassette, body)
#
#         if decision.response is not None:               # REPLAY
#             return _to_httpx_response(decision.response)
#
#         real = self._real.handle_request(request)       # PASS THROUGH
#         # TODO: buffer the response so we can both record it and return it.
#         #   - non-stream: read content, record JSON, return a fresh Response
#         #   - stream:     tee the SSE iterator, record each event, re-emit
#         recorded = _capture(real)                       # RECORD
#         self._cassette.record(request.method, str(request.url), body, recorded)
#         return real
#
# def _to_httpx_response(resp: Response) -> "httpx.Response": ...   # incl. SSE re-emit
# def _capture(real: "httpx.Response") -> Response: ...             # incl. SSE tee
#
# Async is the same logic against httpx.AsyncBaseTransport (roadmap).
