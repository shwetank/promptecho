"""The pytest plugin: auto-named cassette per test, marker configuration,
CI-mode defaulting — exercised end-to-end through pytester, including an
actual record/replay cycle through the fixture."""

import promptecho.pytest_plugin as plugin


def test_fixture_auto_names_cassette(pytester):
    pytester.makepyfile(
        test_inner="""
        def test_summarize(promptecho_cassette):
            # fixture resolves and names the cassette after the test
            assert promptecho_cassette.path.replace("\\\\", "/").endswith(
                "cassettes/test_summarize.yaml"
            )
        """
    )
    result = pytester.runpytest()  # plugin auto-loads via its entry point
    result.assert_outcomes(passed=1)


def test_fixture_records_then_replays(pytester, monkeypatch):
    """The fixture must actually drive a record/replay cycle, not just name a
    path: first pytest run records against a live local server, the second run
    replays with the server shut down."""
    monkeypatch.delenv("CI", raising=False)  # default mode must be 'once'
    pytester.makepyfile(
        test_inner="""
        import json, threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import httpx, pytest

        PORT_FILE = "port.txt"

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                b = json.dumps({"text": "ok"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            def log_message(self, *a): pass

        def test_roundtrip(promptecho_cassette):
            import os
            if os.path.exists("recorded.flag"):
                base = "http://127.0.0.1:9"      # dead port — must replay
            else:
                srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{srv.server_address[1]}"
            r = httpx.Client().post(f"{base}/x", json={"model": "m", "messages": []})
            assert r.json() == {"text": "ok"}
            open("recorded.flag", "w").write("1")
        """
    )
    pytester.runpytest().assert_outcomes(passed=1)   # records
    pytester.runpytest().assert_outcomes(passed=1)   # replays, server dead


def test_marker_configures_fixture(pytester, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    pytester.makepyfile(
        test_inner="""
        import pytest

        @pytest.mark.promptecho(mode="none", match_on=["model"])
        def test_marked(promptecho_cassette):
            assert promptecho_cassette.match_on == ["model"]
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_marker_rejects_unknown_kwargs(pytester, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    pytester.makepyfile(
        test_inner="""
        import pytest

        @pytest.mark.promptecho(record_mode="once")   # wrong name (vcrpy-ism)
        def test_typo(promptecho_cassette):
            pass
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*record_mode*"])


def test_marker_is_registered(pytester):
    result = pytester.runpytest("--markers")
    result.stdout.fnmatch_lines(["*promptecho(mode=*"])


def test_ci_detection_handles_falsey_strings(monkeypatch):
    from promptecho.transport import Mode

    for value, expected in [
        ("true", Mode.NONE), ("1", Mode.NONE), ("yes", Mode.NONE),
        ("false", Mode.ONCE), ("0", Mode.ONCE), ("", Mode.ONCE), ("no", Mode.ONCE),
    ]:
        monkeypatch.setenv("CI", value)
        assert plugin._default_mode() is expected, f"CI={value!r}"
    monkeypatch.delenv("CI")
    assert plugin._default_mode() is Mode.ONCE
