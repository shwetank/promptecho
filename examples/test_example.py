"""What using promptecho feels like with a real SDK. The cassette next door was
recorded once; this replays it with no network and no tokens.

Runs only if the `anthropic` package is installed (`pip install promptecho[dev]`);
skipped otherwise. The cassette matches on model+messages, so no API key is
needed and no request leaves the machine.
"""

import os

import pytest

import promptecho

anthropic = pytest.importorskip("anthropic")

# Resolve relative to this file so the test passes from any working directory.
CASSETTE = os.path.join(os.path.dirname(__file__), "cassettes", "example.yaml")


@promptecho.use_cassette(CASSETTE, mode="none")
def test_summarize_replays_from_cassette():
    client = anthropic.Anthropic(api_key="not-needed-for-replay")
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[{"role": "user", "content": "Summarize: the cat sat on the mat."}],
    )
    assert "cat" in msg.content[0].text.lower()
