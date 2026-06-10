"""Cassette: the on-disk record of interactions. Human-readable YAML.

A cassette is a list of (request, response) interactions keyed by request
fingerprint. It is designed to diff cleanly in PRs and to be safe to commit
(secrets are redacted before anything reaches disk).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import yaml

from .matcher import DEFAULT_MATCH_ON, fingerprint

REDACTED = "REDACTED"
REDACT_HEADERS = {"authorization", "x-api-key", "openai-organization"}

# v2: match keys cover the HTTP method and URL path (and non-JSON bodies are
# keyed by a raw-byte hash), so v1 keys can never match — refuse to load them.
CASSETTE_VERSION = 2


class PromptechoRecordingWarning(UserWarning):
    """Emitted when promptecho records a non-2xx response.

    A transient 401/429/5xx during recording (e.g. an expired key, a rate
    limit, an upstream blip) is baked into the cassette and replays identically
    on every subsequent run — often silently masked by the app's own
    retry/degrade logic, producing green tests over poisoned fixtures.

    The default ``on_record_error='warn'`` policy surfaces every such recording.
    To convert these to hard errors across a whole test suite::

        import warnings
        from promptecho import PromptechoRecordingWarning
        warnings.filterwarnings("error", category=PromptechoRecordingWarning)

    To silence (for example, in tests that intentionally record 429s to verify
    retry logic) pass ``on_record_error='record'`` to ``use_cassette``.
    """


@dataclass
class Response:
    status: int
    headers: dict
    streaming: bool = False
    body: object | None = None          # non-streaming body: JSON, str, or base64 str when binary
    events: list[str] = field(default_factory=list)  # ordered raw SSE events
    binary: bool = False                # if True, body is base64-encoded raw bytes


@dataclass
class Interaction:
    method: str
    url: str
    match_key: str
    matched_on: list[str]
    body: dict
    response: Response


@dataclass
class Cassette:
    path: str
    match_on: list[str] = field(default_factory=lambda: list(DEFAULT_MATCH_ON))
    interactions: list[Interaction] = field(default_factory=list)
    _dirty: bool = False

    # --- lookup -----------------------------------------------------------
    def find(self, key: str) -> Interaction | None:
        for ix in self.interactions:
            if ix.match_key == key:
                return ix
        return None

    def record(self, method: str, url: str, body: dict, response: Response) -> None:
        key = fingerprint(body, self.match_on, method=method, path=urlsplit(url).path)
        self.interactions.append(
            Interaction(
                method=method,
                url=url,
                match_key=key,
                matched_on=list(self.match_on),
                body=body,
                response=_redact_response(response),
            )
        )
        self._dirty = True

    # --- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str, match_on: list[str] | None = None) -> "Cassette":
        mo = match_on or list(DEFAULT_MATCH_ON)
        if not os.path.exists(path):
            return cls(path=path, match_on=mo)
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        version = raw.get("version", 1)
        if raw and version != CASSETTE_VERSION:
            raise ValueError(
                f"{path!r} is a promptecho cassette with format version {version}; "
                f"this promptecho reads version {CASSETTE_VERSION} (match keys now "
                f"include the request method and URL path). Delete the cassette "
                f"and re-record."
            )
        interactions = [_interaction_from_dict(d) for d in raw.get("interactions", [])]
        return cls(path=path, match_on=raw.get("match_on", mo), interactions=interactions)

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        doc = {
            "version": CASSETTE_VERSION,
            "match_on": self.match_on,
            "interactions": [_interaction_to_dict(ix) for ix in self.interactions],
        }
        with open(self.path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=100)
        self._dirty = False


# --- (de)serialization helpers -------------------------------------------
def _redact_response(resp: Response) -> Response:
    headers = {k: (REDACTED if k.lower() in REDACT_HEADERS else v) for k, v in resp.headers.items()}
    return dataclasses.replace(resp, headers=headers)


def _interaction_to_dict(ix: Interaction) -> dict:
    r = ix.response
    response = {"status": r.status, "headers": r.headers, "streaming": r.streaming}
    if r.binary:
        response["binary"] = True
    if r.streaming:
        response["events"] = r.events
    else:
        response["body"] = r.body
    return {
        "request": {
            "method": ix.method,
            "url": ix.url,
            "match_key": ix.match_key,
            "matched_on": ix.matched_on,
            "body": ix.body,
        },
        "response": response,
    }


def _interaction_from_dict(d: dict) -> Interaction:
    req, resp = d["request"], d["response"]
    return Interaction(
        method=req["method"],
        url=req["url"],
        match_key=req["match_key"],
        matched_on=req.get("matched_on", list(DEFAULT_MATCH_ON)),
        body=req.get("body", {}),
        response=Response(
            status=resp["status"],
            headers=resp.get("headers", {}),
            streaming=resp.get("streaming", False),
            body=resp.get("body"),
            events=resp.get("events", []),
            binary=resp.get("binary", False),
        ),
    )
