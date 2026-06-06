"""Per-provider request normalization.

Different providers (and even one SDK across versions) express the *same logical
prompt* in different shapes:

  - Anthropic puts the system prompt in a top-level ``system`` param; OpenAI puts
    it in a ``system``/``developer`` role message.
  - Message content may be a bare string or a list of typed content blocks.
  - Tool defs differ: Anthropic ``{name, description, input_schema}`` vs OpenAI
    ``{type: function, function: {name, description, parameters}}``.

``normalize()`` maps a raw request body into one canonical shape, so logically
identical calls produce the same fingerprint. This is the thing a raw-bytes HTTP
VCR fundamentally cannot do.

The canonical body is also what gets written to the cassette — a provider-agnostic
view of the call, which is arguably more readable than the raw provider JSON.
"""

from __future__ import annotations

from urllib.parse import urlsplit

CANONICAL_ROLES_AS_SYSTEM = ("system", "developer")


# --- detection ------------------------------------------------------------
def detect(url: str, body: dict) -> str:
    """Best-effort provider detection: URL host first, then body shape."""
    host = (urlsplit(url).hostname or "").lower()
    if "anthropic" in host:
        return "anthropic"
    if "openai" in host or "azure" in host:
        return "openai"

    # Shape fallback (covers localhost / proxies / gateways).
    if "system" in body:
        return "anthropic"
    messages = body.get("messages") or []
    if any(isinstance(m, dict) and m.get("role") in CANONICAL_ROLES_AS_SYSTEM for m in messages):
        return "openai"
    tools = body.get("tools") or []
    if any(isinstance(t, dict) and "input_schema" in t for t in tools):
        return "anthropic"
    if any(isinstance(t, dict) and "function" in t for t in tools):
        return "openai"
    return "generic"


# --- shared canonicalizers ------------------------------------------------
def _canon_content(content):
    """Collapse a single text block to a bare string; canonicalize text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [_canon_block(b) for b in content]
        if len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
            return blocks[0]["text"]
        return blocks
    return content


def _canon_block(block):
    if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
        return {"type": "text", "text": block["text"]}
    return block


def _canon_message(message):
    if not isinstance(message, dict):
        return message
    out = dict(message)
    if "content" in out:
        out["content"] = _canon_content(out["content"])
    return out


def _canon_system(system):
    if isinstance(system, list):
        texts = [b.get("text", "") for b in system
                 if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return system


def _canon_tool(tool):
    if not isinstance(tool, dict):
        return tool
    if isinstance(tool.get("function"), dict):          # OpenAI shape
        fn = tool["function"]
        canon = {"name": fn.get("name"), "description": fn.get("description"),
                 "parameters": fn.get("parameters")}
    else:                                               # Anthropic / generic
        canon = {"name": tool.get("name"), "description": tool.get("description"),
                 "parameters": tool.get("input_schema", tool.get("parameters"))}
    return {k: v for k, v in canon.items() if v is not None}


def _canon_tool_choice(choice):
    if isinstance(choice, str):
        return {"mode": choice}
    if isinstance(choice, dict):
        if isinstance(choice.get("function"), dict):    # OpenAI
            return {"mode": "tool", "name": choice["function"].get("name")}
        if "type" in choice:                            # Anthropic
            out = {"mode": choice["type"]}
            if choice.get("name"):
                out["name"] = choice["name"]
            return out
    return choice


def _apply_common(out: dict) -> dict:
    if "messages" in out:
        out["messages"] = [_canon_message(m) for m in out["messages"]]
    if "tools" in out:
        out["tools"] = [_canon_tool(t) for t in out["tools"]]
    if "tool_choice" in out:
        out["tool_choice"] = _canon_tool_choice(out["tool_choice"])
    return out


# --- per-provider ---------------------------------------------------------
def _anthropic(body: dict) -> dict:
    out = dict(body)
    if "system" in out:
        out["system"] = _canon_system(out["system"])
    return _apply_common(out)


def _openai(body: dict) -> dict:
    out = dict(body)
    system_parts, rest = [], []
    for m in out.get("messages", []):
        if isinstance(m, dict) and m.get("role") in CANONICAL_ROLES_AS_SYSTEM:
            c = m.get("content", "")
            system_parts.append(c if isinstance(c, str) else _canon_content(c))
        else:
            rest.append(m)
    if system_parts and "system" not in out:
        out["system"] = "\n".join(p for p in system_parts if isinstance(p, str))
    out["messages"] = rest
    if "max_completion_tokens" in out and "max_tokens" not in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
    return _apply_common(out)


def _generic(body: dict) -> dict:
    return _apply_common(dict(body))


_NORMALIZERS = {"anthropic": _anthropic, "openai": _openai, "generic": _generic}


def normalize(url: str, body: dict) -> dict:
    """Map a raw provider request body into the canonical shape used for matching."""
    if not isinstance(body, dict):
        return body
    return _NORMALIZERS[detect(url, body)](body)
