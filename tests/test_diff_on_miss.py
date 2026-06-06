"""Field-level diff on cassette miss — the CI-quality differentiator."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import promptecho
from promptecho.matcher import diff_request
from promptecho.transport import CassetteMiss


# --- unit: diff_request --------------------------------------------------

MATCH = ["model", "messages", "system", "temperature", "tools"]


def test_top_level_scalar_diff():
    a = {"model": "claude-opus-4-8", "messages": []}
    b = {"model": "claude-haiku-4-5", "messages": []}
    out = diff_request(a, b, MATCH)
    assert "model:" in out
    assert "recorded: claude-haiku-4-5" in out
    assert "incoming: claude-opus-4-8" in out


def test_nested_message_content_diff_with_path():
    a = {"model": "m", "messages": [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "summarize the dog"},
    ]}
    b = {"model": "m", "messages": [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "summarize the cat"},
    ]}
    out = diff_request(a, b, MATCH)
    # The diff must point at messages[1].content specifically, not just "messages"
    assert "messages[1].content:" in out
    assert "summarize the dog" in out
    assert "summarize the cat" in out
    # Unchanged messages[0] must not appear
    assert "messages[0]" not in out


def test_long_strings_get_truncated():
    long_a = "x" * 200
    long_b = "y" * 200
    a = {"model": "m", "messages": [{"role": "user", "content": long_a}]}
    b = {"model": "m", "messages": [{"role": "user", "content": long_b}]}
    out = diff_request(a, b, MATCH)
    assert "..." in out
    # No single rendered line should be massively longer than the truncation limit
    for line in out.splitlines():
        assert len(line) < 120, f"line too long: {line!r}"


def test_missing_field_indicated_on_each_side():
    a = {"model": "m", "messages": [], "temperature": 0.0}
    b = {"model": "m", "messages": []}                       # no temperature
    out = diff_request(a, b, MATCH)
    assert "temperature:" in out
    assert "<not present>" in out


def test_list_length_change():
    a = {"model": "m", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]}
    b = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = diff_request(a, b, MATCH)
    assert "messages[1]" in out


def test_volatile_field_outside_match_on_is_hidden():
    """A field not in match_on can never have caused a miss — don't surface it."""
    a = {"model": "m", "messages": [], "request_id": "abc"}
    b = {"model": "m", "messages": [], "request_id": "xyz"}
    assert diff_request(a, b, MATCH) == ""


def test_identical_bodies_yield_empty_diff():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert diff_request(a, a, MATCH) == ""


# --- integration: error surfaces through a real CassetteMiss --------------

class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_cassette_miss_escapes_except_exception(server, tmp_path):
    """The OpenAI / Anthropic / Mistral SDKs all do ``except Exception: raise
    APIConnectionError(...)`` inside their transport. If CassetteMiss inherits
    from Exception, the SDK swallows it and the diff message is hidden at the
    top of pytest's failure summary. Inheriting from BaseException (as
    pytest.fail's Failed does) bypasses this.
    """
    _, base = server
    cassette = str(tmp_path / "empty.yaml")

    sdk_wrapper_swallowed = False
    try:
        with promptecho.use_cassette(cassette, mode="none"):
            try:
                httpx.Client().post(f"{base}/x", json={"model": "m", "messages": []})
            except Exception:  # the SDK pattern — must NOT catch CassetteMiss
                sdk_wrapper_swallowed = True
    except CassetteMiss:
        pass  # correct path — bypassed the SDK's `except Exception:`

    assert not sdk_wrapper_swallowed, (
        "CassetteMiss was caught by `except Exception:` — SDKs will wrap it and "
        "hide the field-level diff. Make sure it inherits from BaseException."
    )


def test_cassette_miss_is_exported_at_top_level():
    """Users shouldn't have to drill into promptecho.transport."""
    assert promptecho.CassetteMiss is CassetteMiss


def test_real_miss_error_pinpoints_changed_field(server, tmp_path):
    """Record one prompt; then send a slightly-changed one; the error must point at it."""
    srv, base = server
    cassette = str(tmp_path / "miss.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/x", json={
            "model": "m",
            "messages": [{"role": "user", "content": "summarize: the cat sat on the mat"}],
        })

    srv.shutdown()

    with pytest.raises(CassetteMiss) as exc:
        with promptecho.use_cassette(cassette, mode="none"):
            httpx.Client().post(f"{base}/x", json={
                "model": "m",
                "messages": [{"role": "user", "content": "summarize: the dog sat on the mat"}],
            })

    msg = str(exc.value)
    # Pinpoints the exact path, shows both values, suggests the fix.
    assert "messages[0].content" in msg
    assert "the cat sat on the mat" in msg
    assert "the dog sat on the mat" in msg
    assert "re-record" in msg.lower()
