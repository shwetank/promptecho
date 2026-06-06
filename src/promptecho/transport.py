"""The replay/record decision logic, isolated from httpx patching mechanics.

This module is pure and unit-testable: given a mode, a cassette, and a parsed
request, it decides whether to replay a recorded response or pass through to the
network and record. The actual httpx wiring (next to TODOs) lives at the bottom.
"""

from __future__ import annotations

import json
from enum import Enum

from .cassette import Cassette, Response
from .matcher import diff_request, fingerprint


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
        return (
            f"Cassette miss: {cassette.path!r} has no recordings and mode=none.\n"
            f"Re-record with mode='once' (or delete and re-run the test)."
        )
    diff = diff_request(body, nearest.body, cassette.match_on)
    if not diff:
        return (
            f"Cassette miss in {cassette.path!r} (mode=none): a matched field has a "
            f"non-equal value the diff walker couldn't pinpoint. Re-record to refresh."
        )
    return (
        f"Cassette miss in {cassette.path!r} (mode=none).\n"
        f"The incoming request differs from the nearest recording on these fields:\n\n"
        f"{diff}\n\n"
        f"If the change is intentional, re-record with mode='once' (or delete the "
        f"cassette and re-run). If not, fix the call so it matches the recorded "
        f"fingerprint."
    )


# The httpx wiring that turns these decisions into real interception lives in
# patch.py (sync + async). This module stays pure so the branch logic above is
# unit-testable without a network stack.
