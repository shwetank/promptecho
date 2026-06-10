"""The replay/record decision logic, isolated from httpx patching mechanics.

This module is pure and unit-testable: given a mode, a cassette, and a parsed
request, it decides whether to replay a recorded response or pass through to the
network and record. The actual httpx wiring (next to TODOs) lives at the bottom.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from urllib.parse import urlsplit

from .cassette import Cassette, Response
from .matcher import RAW_BODY_KEY, diff_request, fingerprint


class Mode(str, Enum):
    ONCE = "once"            # record if absent, replay if present (default)
    NONE = "none"           # replay only; error on miss (CI-safe)
    NEW_EPISODES = "new_episodes"  # replay existing, record new
    ALL = "all"             # always re-record


class RecordedErrorResponse(BaseException):
    """Raised when ``on_record_error='raise'`` and the upstream returns >= 400.

    Inherits from ``BaseException`` (not ``Exception``) for the same reason as
    ``CassetteMiss``: LLM SDK transport layers wrap any ``Exception`` in their
    own connection-error type (e.g. ``openai.APIConnectionError``), which
    hides the diagnostic at the top of pytest's failure summary. Bypassing
    ``except Exception:`` ensures the message reaches the test runner intact.

    Catch with ``except RecordedErrorResponse:`` or
    ``pytest.raises(RecordedErrorResponse)`` if you genuinely need to.
    """


class CassetteMiss(BaseException):
    """Raised in mode=none when an incoming request has no recording.

    Inherits from ``BaseException`` (not ``Exception``) on purpose. LLM SDKs
    routinely wrap any caught ``Exception`` from their transport layer into
    their own connection-error type — e.g. the OpenAI SDK turns it into
    ``openai.APIConnectionError("Connection error.")``, hiding the real
    field-level diff at the top of pytest's failure summary. Bypassing
    ``except Exception:`` blocks (the same trick ``pytest.fail`` uses for its
    internal ``Failed`` exception) ensures the diagnostic message reaches the
    test runner unmangled.

    If you legitimately need to catch it in a test, use ``except CassetteMiss:``
    or ``pytest.raises(CassetteMiss)`` — both still work.
    """


def parse_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # Non-JSON (multipart, form-encoded, binary) or non-object JSON: key the
    # request by a hash of its exact bytes. Mapping these to {} would make
    # every such request share one fingerprint and silently replay whichever
    # recording landed first.
    return {RAW_BODY_KEY: hashlib.sha256(raw).hexdigest()}


class Decision:
    """Outcome of looking at one request: either replay `response`, or `record`."""

    def __init__(self, *, response: Response | None = None, record: bool = False):
        self.response = response
        self.record = record


def decide(mode: Mode, cassette: Cassette, body: dict, method: str = "", path: str = "") -> Decision:
    """Core branch. No I/O — caller performs the network call / persistence."""
    key = fingerprint(body, cassette.match_on, method=method, path=path)
    existing = cassette.find(key)

    if mode is Mode.ALL:
        return Decision(record=True)

    if mode is Mode.NONE:
        if existing is None:
            raise CassetteMiss(_miss_message(cassette, body, method, path))
        return Decision(response=existing.response)

    # ONCE and NEW_EPISODES: replay if we have it, otherwise record.
    if existing is not None:
        return Decision(response=existing.response)
    return Decision(record=True)


def _miss_message(cassette: Cassette, body: dict, method: str = "", path: str = "") -> str:
    nearest = cassette.interactions[-1] if cassette.interactions else None
    if nearest is None:
        return (
            f"Cassette miss: {cassette.path!r} has no recordings and mode=none.\n"
            f"Re-record with mode='once' (or delete and re-run the test)."
        )
    diff = diff_request(body, nearest.body, cassette.match_on)
    extra = []
    if method and nearest.method and method.upper() != nearest.method.upper():
        extra.append(f"  method:\n    recorded: {nearest.method}\n    incoming: {method}")
    recorded_path = urlsplit(nearest.url).path
    if path and recorded_path != path:
        extra.append(f"  url path:\n    recorded: {recorded_path}\n    incoming: {path}")
    if (RAW_BODY_KEY in body or RAW_BODY_KEY in nearest.body) and \
            body.get(RAW_BODY_KEY) != nearest.body.get(RAW_BODY_KEY):
        extra.append("  body: non-JSON request bodies differ (matched by raw-byte hash)")
    diff = "\n".join(filter(None, [diff, *extra]))
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
