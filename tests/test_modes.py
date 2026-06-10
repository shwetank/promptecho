"""Nested-activation guard: promptecho patches httpx process-wide, so two
simultaneously active cassettes must fail loudly, not interleave recordings."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import promptecho

HITS = {"n": 0}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        HITS["n"] += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        payload = json.dumps({"hit": HITS["n"]}).encode()
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


def _post(base, content):
    return httpx.Client().post(
        f"{base}/x", json={"model": "m", "messages": [{"role": "user", "content": content}]}
    )


def test_nested_cassettes_raise_loudly(tmp_path):
    with promptecho.use_cassette(str(tmp_path / "outer.yaml")):
        with pytest.raises(RuntimeError) as exc:
            with promptecho.use_cassette(str(tmp_path / "inner.yaml")):
                pass
    assert "already active" in str(exc.value)
    assert "outer.yaml" in str(exc.value)


def test_patch_restored_after_nested_failure(server, tmp_path):
    """The failed inner activation must not unpatch or corrupt the outer one."""
    srv, base = server
    cassette = str(tmp_path / "outer.yaml")
    with promptecho.use_cassette(cassette):
        try:
            with promptecho.use_cassette(str(tmp_path / "inner.yaml")):
                pass
        except RuntimeError:
            pass
        _post(base, "still records through the outer cassette")
    srv.shutdown()
    with promptecho.use_cassette(cassette, mode="none"):
        assert _post(base, "still records through the outer cassette").status_code == 200
