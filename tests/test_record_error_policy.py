"""on_record_error policy — gate against silently baking 4xx/5xx into cassettes.

Found by an external contributor's spike: a transient/expired-key error during
recording would replay identically and get masked by app-level retry/degrade
logic, producing green tests over poisoned fixtures.
"""

import json
import os
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import promptecho
from promptecho import PromptechoRecordingWarning, RecordedErrorResponse


# --- servers --------------------------------------------------------------

def _make_server(status: int, body_dict: dict):
    body = json.dumps(body_dict).encode()

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def error_401():
    srv, base = _make_server(401, {"error": "invalid_api_key"})
    yield base
    srv.shutdown()


@pytest.fixture
def ok_200():
    srv, base = _make_server(200, {"content": [{"type": "text", "text": "ok"}]})
    yield base
    srv.shutdown()


# --- the three policies ---------------------------------------------------

def test_default_warns_on_4xx_recording(error_401, tmp_path):
    """Default policy ('warn') surfaces the footgun without breaking flow."""
    cassette = str(tmp_path / "err.yaml")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with promptecho.use_cassette(cassette, mode="once"):
            httpx.Client().post(f"{error_401}/x", json={"model": "m", "messages": []})

    matching = [w for w in captured if issubclass(w.category, PromptechoRecordingWarning)]
    assert len(matching) == 1
    msg = str(matching[0].message)
    assert "401" in msg
    assert cassette in msg
    # The cassette IS written under 'warn' — the user can inspect it.
    assert os.path.exists(cassette)


def test_raise_blocks_the_recording(error_401, tmp_path):
    """on_record_error='raise' aborts BEFORE the poisoned cassette hits disk."""
    cassette = str(tmp_path / "err.yaml")
    with pytest.raises(RecordedErrorResponse) as exc:
        with promptecho.use_cassette(cassette, mode="once", on_record_error="raise"):
            httpx.Client().post(f"{error_401}/x", json={"model": "m", "messages": []})

    assert "401" in str(exc.value)
    assert "NOT written" in str(exc.value)
    # The poisoned cassette must not exist on disk.
    assert not os.path.exists(cassette), "raise mode must not leak the recording to disk"


def test_record_preserves_silent_v0_1_x_behavior(error_401, tmp_path):
    """on_record_error='record' is the legacy escape hatch for tests that
    LEGITIMATELY want to capture an error (e.g. asserting 429 retry behavior)."""
    cassette = str(tmp_path / "err.yaml")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with promptecho.use_cassette(cassette, mode="once", on_record_error="record"):
            httpx.Client().post(f"{error_401}/x", json={"model": "m", "messages": []})

    matching = [w for w in captured if issubclass(w.category, PromptechoRecordingWarning)]
    assert matching == []
    assert os.path.exists(cassette)


# --- standard Python integration patterns --------------------------------

def test_warning_filterable_to_error_for_strict_ci(error_401, tmp_path):
    """The standard Python pattern: convert warning to error project-wide."""
    cassette = str(tmp_path / "err.yaml")
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=PromptechoRecordingWarning)
        with pytest.raises(PromptechoRecordingWarning):
            with promptecho.use_cassette(cassette, mode="once"):
                httpx.Client().post(f"{error_401}/x", json={"model": "m", "messages": []})


def test_2xx_never_triggers_policy(ok_200, tmp_path):
    """A successful response must not emit a warning under ANY policy."""
    cassette = str(tmp_path / "ok.yaml")
    for policy in ("record", "warn", "raise"):
        if os.path.exists(cassette):
            os.remove(cassette)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with promptecho.use_cassette(cassette, mode="once", on_record_error=policy):
                httpx.Client().post(f"{ok_200}/x", json={"model": "m", "messages": []})
        matching = [w for w in captured if issubclass(w.category, PromptechoRecordingWarning)]
        assert matching == [], f"policy={policy!r} falsely warned on a 2xx response"


def test_5xx_also_caught_not_just_4xx(tmp_path):
    """Server errors are exactly the case where this matters most."""
    srv, base = _make_server(503, {"error": "service_unavailable"})
    try:
        cassette = str(tmp_path / "err.yaml")
        with pytest.raises(RecordedErrorResponse) as exc:
            with promptecho.use_cassette(cassette, mode="once", on_record_error="raise"):
                httpx.Client().post(f"{base}/x", json={"model": "m", "messages": []})
        assert "503" in str(exc.value)
    finally:
        srv.shutdown()


# --- API surface ---------------------------------------------------------

def test_invalid_policy_rejected_at_construction():
    """Catch typos early, not at the call site."""
    with pytest.raises(ValueError) as exc:
        promptecho.use_cassette("/tmp/x.yaml", on_record_error="warning")  # typo
    assert "on_record_error" in str(exc.value)


def test_public_exports():
    """Users shouldn't have to drill into submodules."""
    assert promptecho.PromptechoRecordingWarning is PromptechoRecordingWarning
    assert promptecho.RecordedErrorResponse is RecordedErrorResponse
    assert issubclass(PromptechoRecordingWarning, UserWarning)
    # RecordedErrorResponse must escape `except Exception:` (SDK wrappers).
    assert issubclass(RecordedErrorResponse, BaseException)
    assert not issubclass(RecordedErrorResponse, Exception)
