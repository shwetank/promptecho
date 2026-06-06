"""Reasoning-model match defaults + binary response round-trip."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import promptecho
from promptecho.matcher import DEFAULT_MATCH_ON, fingerprint

PNG = bytes([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    0xff, 0xfe, 0x00, 0x01, 0x80, 0x7f, 0xc3, 0x28,
])
HITS = {"n": 0}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        HITS["n"] += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/image":
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        else:
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    HITS["n"] = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# --- reasoning models: match_on defaults must split by reasoning knobs ----

def test_reasoning_effort_distinguishes_calls():
    """`reasoning_effort=high` and `low` must NOT collide on the same recording."""
    base = {"model": "o3-mini",
            "messages": [{"role": "user", "content": "solve: 2+2"}]}
    high = {**base, "reasoning_effort": "high"}
    low = {**base, "reasoning_effort": "low"}
    assert fingerprint(high) != fingerprint(low)


def test_anthropic_thinking_distinguishes_calls():
    base = {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "hi"}]}
    on = {**base, "thinking": {"type": "enabled", "budget_tokens": 4000}}
    off = {**base, "thinking": {"type": "disabled"}}
    assert fingerprint(on) != fingerprint(off)


def test_reasoning_fields_in_defaults():
    for f in ("reasoning_effort", "reasoning", "thinking"):
        assert f in DEFAULT_MATCH_ON


# --- binary response: byte-exact round trip ------------------------------

def test_binary_image_response_byte_exact(server, tmp_path):
    """Record a raw image/png response; replay must return the exact bytes."""
    srv, base = server
    cassette = str(tmp_path / "image.yaml")
    body = {"model": "image-model",
            "messages": [{"role": "user", "content": "draw"}]}

    with promptecho.use_cassette(cassette, mode="once"):
        r = httpx.Client().post(f"{base}/image", json=body)
    assert r.content == PNG, "recorded response should reach the SDK unchanged"
    assert HITS["n"] == 1

    srv.shutdown()  # replay must not touch the network

    with promptecho.use_cassette(cassette, mode="none"):
        r2 = httpx.Client().post(f"{base}/image", json=body)
    assert r2.content == PNG, "replay must round-trip the raw image bytes"
    assert HITS["n"] == 1


def test_base64_in_json_still_works(server, tmp_path):
    """Multimodal-as-JSON (base64 inside content blocks) was already fine; keep it that way."""
    import base64 as b64
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type(
        "H", (BaseHTTPRequestHandler,),
        {"do_POST": lambda self: (
            self.rfile.read(int(self.headers.get("content-length", 0))),
            self.send_response(200),
            self.send_header("content-type", "application/json"),
            self.end_headers(),
            self.wfile.write(json.dumps({
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "data": b64.b64encode(PNG).decode()}},
                    {"type": "text", "text": "here you go"},
                ]
            }).encode()),
        ), "log_message": lambda self, *a: None}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cassette = str(tmp_path / "b64.yaml")
    body = {"model": "m", "messages": [{"role": "user", "content": "draw"}]}

    with promptecho.use_cassette(cassette, mode="once"):
        recorded = httpx.Client().post(f"{base}/x", json=body).json()
    srv.shutdown()
    with promptecho.use_cassette(cassette, mode="none"):
        replayed = httpx.Client().post(f"{base}/x", json=body).json()

    assert recorded == replayed
    decoded = b64.b64decode(replayed["content"][0]["source"]["data"])
    assert decoded == PNG
