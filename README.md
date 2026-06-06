# tapedeck

**Record & replay for LLM API calls.** Like [`vcrpy`](https://github.com/kevin1024/vcrpy)/[`nock`](https://github.com/nock/nock), but built for the way LLM requests actually behave.

Your LLM tests have three problems: they're **flaky** (non-deterministic outputs), **slow** (real network round-trips), and **expensive** (burning tokens in CI on every run). tapedeck records each real API call once to a `cassette` file, then replays it forever — deterministically, instantly, for free.

```python
import tapedeck
from anthropic import Anthropic

@tapedeck.use_cassette("cassettes/summarize.yaml")
def test_summarize():
    client = Anthropic()
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[{"role": "user", "content": "Summarize: the cat sat on the mat."}],
    )
    assert "cat" in msg.content[0].text.lower()
```

First run: one real call, recorded to `cassettes/summarize.yaml`.
Every run after: replayed from disk. No network, no tokens, no flake.

---

## Why not just use vcrpy?

You can — at the HTTP layer, vcrpy works on LLM calls today. tapedeck exists because LLM traffic breaks vcrpy's assumptions in three specific ways:

1. **Matching.** vcrpy matches on raw request bytes. LLM bodies carry volatile fields (client-injected IDs, reordered tools, whitespace) that change the bytes without changing the *meaning* — so byte-matching misses on replay. tapedeck matches on a **normalized fingerprint** of the fields that determine the response, and **canonicalizes across providers**: it knows `content: "hi"` equals `content: [{"type":"text","text":"hi"}]`, and that an Anthropic top-level `system` equals an OpenAI `system`-role message. A raw-bytes VCR can't.
2. **Streaming.** Most LLM calls are SSE streams. tapedeck records the event stream and faithfully re-emits it on replay, so `stream=True` and token-by-token iteration work identically against a cassette.
3. **Secrets.** API keys live in headers on every call. tapedeck redacts them by default — a cassette is safe to commit.

## What tapedeck is *not*

- **Not a cache.** Replay matching is exact/normalized and deterministic, on purpose. It does **not** semantically match "different prompt, close enough" — that would put non-determinism back into the harness you're using to remove it. (A separate opt-in fuzzy mode exists for dev-loop convenience; it is never the default and never used in CI.)
- **Not an eval.** It freezes a response so your *surrounding code* is testable. Judging whether the response is *good* is [a different tool](#roadmap).

---

## Install

```bash
pip install tapedeck   # not yet on PyPI — install from source for now
```

Provider-agnostic: works with any client built on `httpx` (Anthropic, OpenAI, and most others), because interception happens at the HTTP transport layer, not in the SDK.

## Usage

### Decorator
```python
@tapedeck.use_cassette("cassettes/foo.yaml")
def test_foo(): ...
```

### Context manager
```python
with tapedeck.use_cassette("cassettes/foo.yaml"):
    client.messages.create(...)
```

### pytest fixture (auto-named per test)
```python
def test_bar(tapedeck_cassette):   # records to cassettes/test_bar.yaml
    client.messages.create(...)
```

### Record modes
Borrowed from vcrpy, so the mental model is free:

| mode | absent cassette | present cassette | use for |
|------|-----------------|------------------|---------|
| `once` *(default)* | record | replay | normal dev |
| `none` | **error** | replay | **CI** — guarantees no live calls |
| `new_episodes` | record | replay + record new | evolving tests |
| `all` | record | re-record everything | refreshing fixtures |

```python
@tapedeck.use_cassette("cassettes/foo.yaml", mode="none")   # CI-safe
```

### Choosing what to match on
```python
@tapedeck.use_cassette(
    "cassettes/foo.yaml",
    match_on=["model", "messages", "system", "tools"],  # default
)
```
Drop `temperature` if your test sweeps it; add `max_tokens` if it matters. The point is *you* decide which request fields define "the same call."

---

## Cassette format

Human-readable YAML, designed to diff cleanly in PRs. See [`examples/cassettes/example.yaml`](examples/cassettes/example.yaml).

```yaml
version: 1
interactions:
  - request:
      method: POST
      url: https://api.anthropic.com/v1/messages
      match_key: ef43f6acaed95b2f        # fingerprint of matched fields
      matched_on: [model, messages, system, tools]
      body:                              # full body, for human inspection
        model: claude-opus-4-8
        messages:
          - {role: user, content: "Summarize: the cat sat on the mat."}
    response:
      status: 200
      headers: {content-type: application/json}
      streaming: false
      body:
        content: [{type: text, text: "A cat sat on a mat."}]
        usage: {input_tokens: 14, output_tokens: 8}
```

Streamed responses store the ordered SSE events under `response.events` with `streaming: true`, and replay re-emits them in order.

---

## Status

Working core, v0.1.0. Records and replays real httpx traffic (sync + async),
including SSE streaming, verified end-to-end by replaying with the upstream
server shut down (`tests/test_record_replay.py`). Not yet published to PyPI.

## Roadmap (build-in-public)

- [x] Design: matching, cassette format, record modes
- [x] httpx sync + async transport interception
- [x] SSE streaming record/replay
- [x] pytest plugin + auto-naming
- [x] per-provider request normalizers (Anthropic / OpenAI / generic)
- [ ] field-level diff on cassette miss in CI
- [ ] `tapedeck lint` — find un-recorded calls in a test suite
- [ ] **`toMatchLLMSnapshot()` sibling** — semantic snapshot assertions on top of recorded calls

## License

MIT
