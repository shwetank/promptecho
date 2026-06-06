"""pytest integration: an auto-named cassette per test.

    def test_summarize(promptecho_cassette):   # -> cassettes/test_summarize.yaml
        client.messages.create(...)

Mode defaults to ``once`` locally and ``none`` in CI (when the CI env var is set),
so a forgotten recording fails the build instead of making a live call.
"""

from __future__ import annotations

import os

import pytest

from . import use_cassette
from .transport import Mode


def _default_mode() -> Mode:
    return Mode.NONE if os.environ.get("CI") else Mode.ONCE


@pytest.fixture
def promptecho_cassette(request):
    cassette_dir = os.path.join(os.path.dirname(request.fspath), "cassettes")
    path = os.path.join(cassette_dir, f"{request.node.name}.yaml")
    with use_cassette(path, mode=_default_mode()) as cassette:
        yield cassette
