"""What using tapedeck feels like. The cassette next door was 'recorded' once;
this test replays it with no network and no tokens.
"""

import tapedeck
from anthropic import Anthropic


@tapedeck.use_cassette("examples/cassettes/example.yaml", mode="none")
def test_summarize_replays_from_cassette():
    client = Anthropic()  # api key not needed: the call never leaves the machine
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[{"role": "user", "content": "Summarize: the cat sat on the mat."}],
    )
    assert "cat" in msg.content[0].text.lower()
