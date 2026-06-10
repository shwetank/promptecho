"""Fingerprint scope: method + URL path are always part of the match key, and
non-JSON bodies are keyed by a hash of their exact bytes.

Without this, two different endpoints called with the same body — or any two
requests whose bodies don't parse as JSON objects (multipart uploads,
form-encoded, GETs with no body) — collide on one fingerprint and silently
replay whichever recording landed first. A testing tool returning the wrong
recorded response without erroring is the worst failure mode it can have.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import promptecho
from promptecho.cassette import Cassette
from promptecho.matcher import RAW_BODY_KEY, fingerprint
from promptecho.transport import CassetteMiss, parse_body

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


# --- unit: fingerprint envelope -------------------------------------------

def test_path_distinguishes_requests():
    assert fingerprint(BODY, method="POST", path="/v1/messages") != \
           fingerprint(BODY, method="POST", path="/v1/embeddings")


def test_method_distinguishes_requests():
    assert fingerprint(BODY, method="GET", path="/v1/models") != \
           fingerprint(BODY, method="POST", path="/v1/models")


def test_method_is_case_insensitive():
    assert fingerprint(BODY, method="post", path="/x") == \
           fingerprint(BODY, method="POST", path="/x")


def test_host_does_not_affect_the_key():
    """Path is in the key; the host is not — record against one gateway (or a
    test server on a random port), replay through another."""
    # fingerprint takes a path, not a URL, so this is by construction; assert
    # the recording side agrees by going through Cassette.record.
    c1, c2 = Cassette(path="a.yaml"), Cassette(path="b.yaml")
    from promptecho.cassette import Response
    r = Response(status=200, headers={})
    c1.record("POST", "https://api.anthropic.com/v1/messages", BODY, r)
    c2.record("POST", "http://127.0.0.1:5000/v1/messages", BODY, r)
    assert c1.interactions[0].match_key == c2.interactions[0].match_key


# --- unit: non-JSON bodies -------------------------------------------------

def test_non_json_bodies_get_distinct_raw_hashes():
    a = parse_body(b"--boundary\r\naudio-bytes-one\r\n--boundary--")
    b = parse_body(b"--boundary\r\naudio-bytes-two\r\n--boundary--")
    assert RAW_BODY_KEY in a and RAW_BODY_KEY in b
    assert a[RAW_BODY_KEY] != b[RAW_BODY_KEY]
    assert fingerprint(a, method="POST", path="/x") != fingerprint(b, method="POST", path="/x")


def test_non_object_json_is_keyed_by_raw_bytes():
    # A JSON array or scalar body has no fields to pick; key it by bytes too.
    assert RAW_BODY_KEY in parse_body(b"[1, 2, 3]")
    assert parse_body(b"[1]")[RAW_BODY_KEY] != parse_body(b"[2]")[RAW_BODY_KEY]


def test_empty_body_still_parses_to_empty_dict():
    assert parse_body(b"") == {}


def test_raw_key_survives_even_when_not_in_match_on():
    a = {RAW_BODY_KEY: "aa"}
    b = {RAW_BODY_KEY: "bb"}
    assert fingerprint(a, ["model"], method="POST", path="/x") != \
           fingerprint(b, ["model"], method="POST", path="/x")


# --- integration: endpoint collision is now a miss, not a wrong replay -----

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        payload = json.dumps({"endpoint": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_same_body_different_endpoints_do_not_collide(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "two_endpoints.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        r1 = httpx.Client().post(f"{base}/chat", json=BODY)
        r2 = httpx.Client().post(f"{base}/score", json=BODY)
    assert (r1.json()["endpoint"], r2.json()["endpoint"]) == ("/chat", "/score")

    srv.shutdown()

    with promptecho.use_cassette(cassette, mode="none"):
        assert httpx.Client().post(f"{base}/chat", json=BODY).json()["endpoint"] == "/chat"
        assert httpx.Client().post(f"{base}/score", json=BODY).json()["endpoint"] == "/score"


def test_unrecorded_path_misses_and_names_the_path(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "one_endpoint.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/chat", json=BODY)
    srv.shutdown()

    with pytest.raises(CassetteMiss) as exc:
        with promptecho.use_cassette(cassette, mode="none"):
            httpx.Client().post(f"{base}/other", json=BODY)
    msg = str(exc.value)
    assert "url path" in msg
    assert "/chat" in msg and "/other" in msg


def test_distinct_multipart_bodies_record_and_replay_distinctly(server, tmp_path):
    """Two non-JSON payloads to the same endpoint must keep separate recordings."""
    srv, base = server
    cassette = str(tmp_path / "raw.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/upload", content=b"raw-payload-one")
        httpx.Client().post(f"{base}/upload", content=b"raw-payload-two")

    srv.shutdown()

    # Each payload replays; an unrecorded third payload misses (no silent
    # wrong-replay) and the error says why.
    with promptecho.use_cassette(cassette, mode="none"):
        assert httpx.Client().post(f"{base}/upload", content=b"raw-payload-one").status_code == 200
        assert httpx.Client().post(f"{base}/upload", content=b"raw-payload-two").status_code == 200
    with pytest.raises(CassetteMiss) as exc:
        with promptecho.use_cassette(cassette, mode="none"):
            httpx.Client().post(f"{base}/upload", content=b"raw-payload-three")
    assert "non-JSON request bodies differ" in str(exc.value)


# --- cassette format version guard -----------------------------------------

def test_v1_cassette_refused_with_clear_error(tmp_path):
    p = tmp_path / "old.yaml"
    p.write_text(
        "version: 1\n"
        "match_on: [model, messages]\n"
        "interactions: []\n"
    )
    with pytest.raises(ValueError) as exc:
        Cassette.load(str(p))
    msg = str(exc.value)
    assert "version 1" in msg
    assert "re-record" in msg.lower()


def test_v2_cassette_round_trips(tmp_path, server):
    srv, base = server
    cassette = str(tmp_path / "v2.yaml")
    with promptecho.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/chat", json=BODY)
    import yaml
    assert yaml.safe_load(open(cassette))["version"] == 2
    # Loads back without complaint.
    assert len(Cassette.load(cassette).interactions) == 1
