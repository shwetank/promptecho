"""End-to-end proof that tapelog records and replays real httpx traffic.

A local HTTP server stands in for an LLM API. We record against it, shut it
down, then replay — if replay still works with the server dead, the response
genuinely came from the cassette and not the network.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import tapelog

# how many times the upstream server was actually hit
HITS = {"n": 0}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        HITS["n"] += 1
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        if self.path == "/json":
            payload = json.dumps(
                {"content": [{"type": "text", "text": "A cat sat on a mat."}]}
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for tok in ["Hello", " world", "!"]:
                self.wfile.write(
                    f"event: token\ndata: {json.dumps({'text': tok})}\n\n".encode()
                )
                self.wfile.flush()

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def server():
    HITS["n"] = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_json_record_then_replay_with_server_down(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "json.yaml")
    body = {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "Summarize: the cat sat on the mat."}]}

    # RECORD: one real call.
    with tapelog.use_cassette(cassette, mode="once"):
        r = httpx.Client().post(f"{base}/json", json=body)
    assert r.json()["content"][0]["text"] == "A cat sat on a mat."
    assert HITS["n"] == 1

    # Kill the server so replay literally cannot reach the network.
    srv.shutdown()

    # REPLAY: same response, zero new hits, server dead.
    with tapelog.use_cassette(cassette, mode="none"):
        r2 = httpx.Client().post(f"{base}/json", json=body)
    assert r2.json()["content"][0]["text"] == "A cat sat on a mat."
    assert HITS["n"] == 1  # no additional upstream call


def test_streaming_record_then_replay(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "sse.yaml")
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}

    def collect():
        with httpx.Client() as c, c.stream("POST", f"{base}/sse", json=body) as resp:
            return [json.loads(line[6:])["text"]
                    for line in resp.iter_lines() if line.startswith("data: ")]

    with tapelog.use_cassette(cassette, mode="once"):
        recorded = collect()
    assert recorded == ["Hello", " world", "!"]
    assert HITS["n"] == 1

    srv.shutdown()

    with tapelog.use_cassette(cassette, mode="none"):
        replayed = collect()
    assert replayed == ["Hello", " world", "!"]
    assert HITS["n"] == 1  # streamed purely from the cassette


def test_async_record_then_replay(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "async.yaml")
    body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}

    async def call():
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base}/json", json=body)
            return r.json()["content"][0]["text"]

    with tapelog.use_cassette(cassette, mode="once"):
        assert asyncio.run(call()) == "A cat sat on a mat."
    assert HITS["n"] == 1

    srv.shutdown()

    with tapelog.use_cassette(cassette, mode="none"):
        assert asyncio.run(call()) == "A cat sat on a mat."
    assert HITS["n"] == 1  # replayed from cassette, async client, server dead


def test_cross_shape_replay(server, tmp_path):
    """Record an Anthropic-shaped call; replay its OpenAI-shaped equivalent."""
    srv, base = server
    cassette = str(tmp_path / "cross.yaml")
    anthropic_body = {"model": "shared", "system": "be terse",
                      "messages": [{"role": "user", "content": "hi"}]}
    openai_body = {"model": "shared",
                   "messages": [{"role": "system", "content": "be terse"},
                                {"role": "user", "content": "hi"}]}

    with tapelog.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/json", json=anthropic_body)
    assert HITS["n"] == 1

    srv.shutdown()  # replay must not touch the network

    with tapelog.use_cassette(cassette, mode="none"):
        r = httpx.Client().post(f"{base}/json", json=openai_body)
    assert r.json()["content"][0]["text"] == "A cat sat on a mat."
    assert HITS["n"] == 1  # matched the Anthropic recording despite different shape


def test_mode_none_miss_raises(server, tmp_path):
    _, base = server
    cassette = str(tmp_path / "empty.yaml")
    from tapelog.transport import CassetteMiss

    with pytest.raises(CassetteMiss):
        with tapelog.use_cassette(cassette, mode="none"):
            httpx.Client().post(f"{base}/json", json={"model": "x", "messages": []})
    assert HITS["n"] == 0  # never went to network


def test_fingerprint_ignores_volatile_fields(server, tmp_path):
    """A re-ordered body with an extra volatile field still hits the recording."""
    srv, base = server
    cassette = str(tmp_path / "fp.yaml")

    with tapelog.use_cassette(cassette, mode="once"):
        httpx.Client().post(f"{base}/json",
                            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert HITS["n"] == 1
    srv.shutdown()

    # different key order + an extra field not in match_on -> same fingerprint -> replay
    with tapelog.use_cassette(cassette, mode="none"):
        r = httpx.Client().post(
            f"{base}/json",
            json={"messages": [{"role": "user", "content": "x"}], "model": "m",
                  "request_id": "vol-123"},
        )
    assert r.status_code == 200
    assert HITS["n"] == 1
