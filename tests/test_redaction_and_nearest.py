"""URL query-string redaction (cassettes must be safe to commit even with
query-param auth) and nearest-recording selection for the miss diff."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import yaml

import promptecho
from promptecho.cassette import REDACT_HEADERS, _redact_url
from promptecho.transport import CassetteMiss


# --- unit: _redact_url ------------------------------------------------------

def test_query_values_redacted_names_kept():
    url = "https://generativelanguage.googleapis.com/v1/models/x:generateContent?key=AIzaSyFAKE123&alt=json"
    out = _redact_url(url)
    assert "AIzaSyFAKE123" not in out
    assert "key=REDACTED" in out and "alt=REDACTED" in out
    assert out.startswith("https://generativelanguage.googleapis.com/v1/models/x:generateContent?")


def test_url_without_query_untouched():
    url = "https://api.anthropic.com/v1/messages"
    assert _redact_url(url) == url


def test_set_cookie_in_redact_set():
    assert "set-cookie" in REDACT_HEADERS


# --- integration: nothing secret reaches disk -------------------------------

class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        b = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.send_header("set-cookie", "session=SECRET-SESSION-TOKEN")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_query_auth_and_cookies_never_reach_the_cassette(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "redact.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        r = httpx.Client().post(
            f"{base}/generate?api_key=sk-SUPER-SECRET&v=2",
            json={"model": "m", "messages": []},
        )
    assert r.json() == {"ok": True}

    text = open(cassette).read()
    assert "sk-SUPER-SECRET" not in text
    assert "SECRET-SESSION-TOKEN" not in text
    doc = yaml.safe_load(text)
    assert doc["interactions"][0]["request"]["url"].endswith("/generate?api_key=REDACTED&v=REDACTED")
    assert doc["interactions"][0]["response"]["headers"]["set-cookie"] == "REDACTED"

    # Redaction must not break replay (query is not part of the match key).
    srv.shutdown()
    with promptecho.use_cassette(cassette, mode="none"):
        r2 = httpx.Client().post(
            f"{base}/generate?api_key=sk-SUPER-SECRET&v=2",
            json={"model": "m", "messages": []},
        )
    assert r2.json() == {"ok": True}


# --- nearest-recording selection for the miss diff --------------------------

def test_miss_diff_picks_most_similar_recording_not_last(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "many.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        for prompt in ["alpha prompt", "summarize: the cat sat on the mat", "zeta prompt"]:
            httpx.Client().post(
                f"{base}/x",
                json={"model": "m", "messages": [{"role": "user", "content": prompt}]},
            )
    srv.shutdown()

    # One word changed from the MIDDLE recording. The diff must be computed
    # against that one — diffing against the last ("zeta prompt") would show a
    # useless whole-string mismatch.
    with pytest.raises(CassetteMiss) as exc:
        with promptecho.use_cassette(cassette, mode="none"):
            httpx.Client().post(
                f"{base}/x",
                json={"model": "m",
                      "messages": [{"role": "user", "content": "summarize: the dog sat on the mat"}]},
            )
    msg = str(exc.value)
    assert "the cat sat on the mat" in msg, "diff must be against the most similar recording"
    assert "the dog sat on the mat" in msg
    assert "zeta prompt" not in msg
