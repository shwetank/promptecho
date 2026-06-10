"""Record modes `new_episodes` and `all`, the PROMPTECHO_MODE override, and the
nested-activation guard. (`once` and `none` are covered by test_record_replay.)
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import yaml

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


def test_new_episodes_replays_existing_and_records_new(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "ep.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        _post(base, "first")
    assert HITS["n"] == 1

    # Existing request replays (no new hit); unseen request records (one hit).
    with promptecho.use_cassette(cassette, mode="new_episodes"):
        r1 = _post(base, "first")
        r2 = _post(base, "second")
    assert r1.json() == {"hit": 1}, "existing recording must replay, not re-record"
    assert HITS["n"] == 2, "only the new episode goes to the network"

    srv.shutdown()

    # Both now replay with the server dead.
    with promptecho.use_cassette(cassette, mode="none"):
        assert _post(base, "first").json() == {"hit": 1}
        assert _post(base, "second").json() == {"hit": 2}
    assert HITS["n"] == 2


def test_all_re_records_from_scratch(server, tmp_path):
    srv, base = server
    cassette = str(tmp_path / "all.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        _post(base, "first")
        _post(base, "stale")
    assert HITS["n"] == 2

    # mode=all wipes the cassette and re-records everything that runs.
    with promptecho.use_cassette(cassette, mode="all"):
        assert _post(base, "first").json() == {"hit": 3}, "must hit the network, not replay"
    assert HITS["n"] == 3

    doc = yaml.safe_load(open(cassette))
    assert len(doc["interactions"]) == 1, "stale recordings must be dropped"

    srv.shutdown()
    with promptecho.use_cassette(cassette, mode="none"):
        assert _post(base, "first").json() == {"hit": 3}


def test_promptecho_mode_env_overrides_explicit_mode(server, tmp_path, monkeypatch):
    """PROMPTECHO_MODE=all must beat even an explicit mode='none' — it's the
    suite-wide re-record switch."""
    srv, base = server
    cassette = str(tmp_path / "env.yaml")

    with promptecho.use_cassette(cassette, mode="once"):
        _post(base, "first")
    assert HITS["n"] == 1

    monkeypatch.setenv("PROMPTECHO_MODE", "all")
    with promptecho.use_cassette(cassette, mode="none"):
        assert _post(base, "first").json() == {"hit": 2}
    assert HITS["n"] == 2

    monkeypatch.delenv("PROMPTECHO_MODE")
    srv.shutdown()
    with promptecho.use_cassette(cassette, mode="none"):
        assert _post(base, "first").json() == {"hit": 2}


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
