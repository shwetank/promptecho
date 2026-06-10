"""pytest integration: an auto-named cassette per test.

    def test_summarize(promptecho_cassette):   # -> cassettes/test_summarize.yaml
        client.messages.create(...)

Mode defaults to ``once`` locally and ``none`` in CI (when the CI env var is
truthy), so a forgotten recording fails the build instead of making a live call.

Configure per test with the ``promptecho`` marker::

    @pytest.mark.promptecho(match_on=["model", "messages", "temperature"])
    def test_sampling(promptecho_cassette): ...

    @pytest.mark.promptecho(mode="new_episodes", on_record_error="raise")
    def test_evolving(promptecho_cassette): ...

Re-record a whole suite without touching code: ``PROMPTECHO_MODE=all pytest``
(the env var overrides every mode, including the marker's — see README).
"""

from __future__ import annotations

import os

import pytest

from . import use_cassette
from .transport import Mode

_MARKER_KWARGS = ("mode", "match_on", "on_record_error")


def _is_ci() -> bool:
    # "CI=false" / "CI=0" must not count as CI; any-non-empty-string is a trap.
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _default_mode() -> Mode:
    return Mode.NONE if _is_ci() else Mode.ONCE


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "promptecho(mode=..., match_on=..., on_record_error=...): "
        "configure the promptecho_cassette fixture for this test.",
    )


@pytest.fixture
def promptecho_cassette(request):
    cassette_dir = os.path.join(os.path.dirname(str(request.fspath)), "cassettes")
    path = os.path.join(cassette_dir, f"{request.node.name}.yaml")

    kwargs = {"mode": _default_mode()}
    marker = request.node.get_closest_marker("promptecho")
    if marker is not None:
        unknown = set(marker.kwargs) - set(_MARKER_KWARGS)
        if marker.args or unknown:
            raise ValueError(
                f"@pytest.mark.promptecho takes keyword arguments {_MARKER_KWARGS}, "
                f"got positional args {marker.args!r} / unknown kwargs {sorted(unknown)!r}"
            )
        kwargs.update(marker.kwargs)

    with use_cassette(path, **kwargs) as cassette:
        yield cassette
