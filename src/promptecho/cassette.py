"""Cassette: the on-disk record of interactions. Human-readable YAML.

A cassette is a list of (request, response) interactions keyed by request
fingerprint. It is designed to diff cleanly in PRs and to be safe to commit
(secrets are redacted before anything reaches disk).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field

import yaml

from .matcher import DEFAULT_MATCH_ON, fingerprint

REDACTED = "REDACTED"
REDACT_HEADERS = {"authorization", "x-api-key", "openai-organization"}


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
        key = fingerprint(body, self.match_on)
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
        interactions = [_interaction_from_dict(d) for d in raw.get("interactions", [])]
        return cls(path=path, match_on=raw.get("match_on", mo), interactions=interactions)

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        doc = {
            "version": 1,
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
