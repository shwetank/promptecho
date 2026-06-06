"""Request fingerprinting — the deterministic core of replay matching.

We match on a *normalized fingerprint* of the request fields that actually
determine the response, never on raw bytes (see DESIGN.md §2).
"""

from __future__ import annotations

import hashlib
import json

DEFAULT_MATCH_ON = ["model", "messages", "system", "tools", "tool_choice"]


def canonical_json(obj: object) -> str:
    """Stable serialization: sorted keys, no insignificant whitespace.

    Ensures re-serialization of the same logical request can't change the key.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pick(body: dict, match_on: list[str]) -> dict:
    """Keep only the load-bearing fields, in a stable shape."""
    return {k: body[k] for k in match_on if k in body}


def fingerprint(body: dict, match_on: list[str] | None = None) -> str:
    """Map a request body to the cassette key for the recording it should replay.

    The same logical request always yields the same key; volatile fields that
    aren't in ``match_on`` cannot affect it.
    """
    fields = pick(body, match_on or DEFAULT_MATCH_ON)
    digest = hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()
    return digest[:16]


def diff_fields(incoming: dict, recorded: dict, match_on: list[str]) -> list[str]:
    """Field-level explanation for a cassette miss (mode=none diagnostics)."""
    changed = []
    for k in match_on:
        if incoming.get(k) != recorded.get(k):
            changed.append(k)
    return changed
