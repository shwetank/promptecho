"""Request fingerprinting — the deterministic core of replay matching.

We match on a *normalized fingerprint* of the request fields that actually
determine the response, never on raw bytes (see DESIGN.md §2).
"""

from __future__ import annotations

import hashlib
import json

DEFAULT_MATCH_ON = [
    "model", "messages", "system", "tools", "tool_choice",
    # Reasoning-model knobs that change the response without changing the prompt:
    # OpenAI o-series, Anthropic extended thinking, OpenRouter unified field.
    # If these aren't matched, "reasoning_effort=high" and "low" tests collide.
    "reasoning_effort", "reasoning", "thinking",
]


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
    """Names of top-level matched fields whose values differ. Cheap pointer; for the
    human-readable leaf-level diff used in cassette-miss errors, see :func:`diff_request`."""
    return [k for k in match_on if incoming.get(k) != recorded.get(k)]


_MISSING = object()  # sentinel for "field/element not present on this side"


def _walk_diff(incoming, recorded, path: str):
    """Yield (path, recorded_value, incoming_value) for each leaf-level difference.

    Walks both structures in parallel. dicts are compared by key (sorted for stable
    output); lists by index, with extras flagged. Anything else is a leaf — emit and
    stop. Recursion depth is bounded by request shape (chat messages rarely nest deep).
    """
    if incoming == recorded:
        return
    if type(incoming) is not type(recorded):
        yield path, recorded, incoming
        return
    if isinstance(incoming, dict):
        for k in sorted(set(incoming) | set(recorded)):
            sub = f"{path}.{k}" if path else k
            i_val = incoming.get(k, _MISSING)
            r_val = recorded.get(k, _MISSING)
            if i_val is _MISSING or r_val is _MISSING:
                yield sub, r_val, i_val
            else:
                yield from _walk_diff(i_val, r_val, sub)
        return
    if isinstance(incoming, list):
        for i in range(max(len(incoming), len(recorded))):
            sub = f"{path}[{i}]"
            if i >= len(incoming):
                yield sub, recorded[i], _MISSING
            elif i >= len(recorded):
                yield sub, _MISSING, incoming[i]
            else:
                yield from _walk_diff(incoming[i], recorded[i], sub)
        return
    yield path, recorded, incoming


def _truncate(v, limit: int = 80) -> str:
    if v is _MISSING:
        return "<not present>"
    s = v if isinstance(v, str) else canonical_json(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def diff_request(incoming: dict, recorded: dict, match_on: list[str]) -> str:
    """Multi-line, human-readable field-level diff of two request bodies.

    Restricted to ``match_on`` fields — volatile fields outside the match set are
    intentionally hidden, since they can't have caused the miss. Returns the empty
    string when no matched fields differ; callers should treat that as "no diff."
    """
    lines = []
    for field in match_on:
        i_val = incoming.get(field, _MISSING)
        r_val = recorded.get(field, _MISSING)
        if i_val is _MISSING and r_val is _MISSING:
            continue
        if i_val == r_val:
            continue
        for path, r, i in _walk_diff(i_val, r_val, field):
            lines.append(f"  {path}:")
            lines.append(f"    recorded: {_truncate(r)}")
            lines.append(f"    incoming: {_truncate(i)}")
    return "\n".join(lines)
