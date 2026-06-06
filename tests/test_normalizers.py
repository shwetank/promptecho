"""The normalizer makes logically-identical calls fingerprint the same, even
when their wire representation differs. This is the capability a raw-bytes HTTP
VCR cannot have.
"""

from tapelog.matcher import fingerprint
from tapelog.normalizers import detect, normalize

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def fp(url, body):
    return fingerprint(normalize(url, body))


def test_string_content_equals_single_text_block():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    b = {"model": "m", "messages": [{"role": "user",
                                     "content": [{"type": "text", "text": "hi"}]}]}
    assert fp(ANTHROPIC_URL, a) == fp(ANTHROPIC_URL, b)


def test_system_param_equals_system_role_message_cross_provider():
    # Same model + system + user turn, expressed in each provider's shape.
    anthropic = {
        "model": "shared-model",
        "system": "You are terse.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    openai = {
        "model": "shared-model",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ],
    }
    assert fp(ANTHROPIC_URL, anthropic) == fp(OPENAI_URL, openai)


def test_openai_developer_role_treated_as_system():
    dev = {"model": "m", "messages": [
        {"role": "developer", "content": "rules"},
        {"role": "user", "content": "hi"}]}
    sys = {"model": "m", "messages": [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"}]}
    assert fp(OPENAI_URL, dev) == fp(OPENAI_URL, sys)


def test_tool_defs_match_across_providers():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    anthropic = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                 "tools": [{"name": "weather", "description": "get weather",
                            "input_schema": schema}]}
    openai = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
              "tools": [{"type": "function", "function": {
                  "name": "weather", "description": "get weather", "parameters": schema}}]}
    assert fp(ANTHROPIC_URL, anthropic) == fp(OPENAI_URL, openai)


def test_max_completion_tokens_aliases_max_tokens():
    canon = normalize(OPENAI_URL, {"model": "m", "messages": [], "max_completion_tokens": 50})
    assert canon["max_tokens"] == 50 and "max_completion_tokens" not in canon


def test_detection():
    assert detect(ANTHROPIC_URL, {}) == "anthropic"
    assert detect(OPENAI_URL, {}) == "openai"
    assert detect("http://localhost/x", {"system": "s", "messages": []}) == "anthropic"
    assert detect("http://localhost/x",
                  {"messages": [{"role": "system", "content": "s"}]}) == "openai"
    assert detect("http://localhost/x", {"messages": [{"role": "user", "content": "h"}]}) == "generic"


def test_different_prompts_still_differ():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    b = {"model": "m", "messages": [{"role": "user", "content": "bye"}]}
    assert fp(ANTHROPIC_URL, a) != fp(ANTHROPIC_URL, b)
