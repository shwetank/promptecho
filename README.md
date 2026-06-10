# promptecho

**Record & replay for LLM API calls.** Like [`vcrpy`](https://github.com/kevin1024/vcrpy) / [`nock`](https://github.com/nock/nock), but built for the way LLM traffic actually behaves.

Your LLM tests have three problems: they're **flaky** (non-deterministic outputs), **slow** (real network round-trips), and **expensive** (burning tokens in CI on every run). promptecho records each real API call once to a cassette file, then replays it forever — deterministically, instantly, for free.

```python
import promptecho
from anthropic import Anthropic

@promptecho.use_cassette("cassettes/summarize.yaml")
def test_summarize():
    client = Anthropic()
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[{"role": "user", "content": "Summarize: the cat sat on the mat."}],
    )
    assert "cat" in msg.content[0].text.lower()
```

First run: one real call, recorded to `cassettes/summarize.yaml` — this needs the provider SDK installed (`pip install anthropic`) and a real `ANTHROPIC_API_KEY` in the environment.
Every run after: replayed from disk. No network, no tokens, no API key, no flake.

> **Proof, not marketing.** The end-to-end test that gates every release records against a local server, **shuts the server down**, then replays. Same response, zero network. If the response can come back with the upstream gone, the cassette is genuinely doing the work — not a partial proxy. See [`tests/test_record_replay.py`](tests/test_record_replay.py).

---

## Why not just use vcrpy?

You can — at the HTTP layer, vcrpy works on LLM calls today. promptecho exists because LLM traffic breaks vcrpy's assumptions in five specific ways:

1. **Matching.** vcrpy matches on raw request bytes. LLM bodies carry volatile fields (client-injected IDs, reordered tools, whitespace) that change the bytes without changing the *meaning* — so byte-matching misses on replay. promptecho matches on a **normalized fingerprint** of the fields that determine the response, and **canonicalizes across providers**: it knows `content: "hi"` equals `content: [{"type":"text","text":"hi"}]`, an Anthropic top-level `system` equals an OpenAI `system`-role message, and an Anthropic `input_schema` tool def equals an OpenAI `function.parameters`. A raw-bytes VCR can't.
2. **Streaming.** Most LLM calls are SSE streams. promptecho records the event stream and faithfully re-emits it on replay, so `stream=True` and token-by-token iteration work identically against a cassette — including reasoning deltas.
3. **Binary / multimodal responses.** vcrpy's text-based cassettes silently corrupt raw `image/*` / `audio/*` / `octet-stream` bodies. promptecho detects them by `Content-Type` and base64-encodes them in the cassette, so image-out and audio-out responses round-trip byte-exact.
4. **Debuggable CI failures.** When a vcrpy cassette miss happens, you get *"no match"*. promptecho prints the exact path that changed: `messages[1].content: recorded "summarize the cat" / incoming "summarize the dog"`. Test failures are actionable, not detective work.
5. **Secrets.** API keys live in headers on every call. promptecho redacts them by default — a cassette is safe to commit.

## What promptecho is *not*

- **Not a cache.** Replay matching is exact/normalized and deterministic, on purpose. It does **not** semantically match "different prompt, close enough" — that would put non-determinism back into the harness you're using to remove it. (A separate opt-in fuzzy mode is on the roadmap as a dev-loop convenience; it will never be the default and never used in CI.)
- **Not an eval.** It freezes a response so your *surrounding code* is testable. Judging whether the response is *good* is a different tool (see roadmap: `toMatchLLMSnapshot()`).

---

## What it covers

promptecho intercepts at the `httpx` transport layer. **If the SDK uses httpx, promptecho sees the call** — which is almost everything modern.

| You're calling | Covered? |
|---|---|
| Anthropic, OpenAI, Mistral, Cohere, `google-genai` SDKs | ✅ |
| **OpenAI SDK with custom `base_url`** → OpenRouter, Together, Fireworks, Cerebras, Groq, DeepInfra, Perplexity | ✅ |
| **Self-hosted vLLM / TGI / SGLang / LM Studio / Ollama** (OpenAI-compatible mode) | ✅ |
| Your **own fine-tune** behind any of the above | ✅ |
| **Reasoning models** — o1/o3, Claude extended thinking, DeepSeek-R1 | ✅ (incl. `reasoning_effort` / `thinking` in default match-on) |
| **Multimodal** — base64-in-JSON (vision, Claude image-out, GPT-4o) and raw binary (`image/*`, `audio/*`) | ✅ (byte-exact round-trip) |
| Bedrock via boto3, HF `InferenceClient`, in-process `transformers` | ❌ (see workarounds in [SUPPORT.md](SUPPORT.md)) |

Full matrix with caveats and workarounds: [**SUPPORT.md**](SUPPORT.md). For practical recipes by scenario (startup / enterprise / research), see [**TUTORIAL.md**](TUTORIAL.md).

### Hosted open-source via the OpenAI SDK

This is the dominant pattern for non-Anthropic/non-OpenAI usage, and it Just Works:

```python
from openai import OpenAI
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key="...")

@promptecho.use_cassette("cassettes/openrouter.yaml")
def test_via_openrouter():
    r = client.chat.completions.create(
        model="meta-llama/llama-3.1-70b-instruct",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert r.choices[0].message.content
```

Detection falls back to body shape when the host is unknown, so localhost gateways, in-house proxies, and self-hosted vLLM/TGI behave the same way as the brand-name hosts.

---

## Install

```bash
pip install promptecho   # not yet on PyPI — install from source for now
```

```bash
git clone <repo> && cd promptecho
pip install -e .
```

Requires Python ≥ 3.9 and `httpx ≥ 0.24`.

---

## Usage

### Decorator
```python
@promptecho.use_cassette("cassettes/foo.yaml")
def test_foo(): ...
```

### Context manager
```python
with promptecho.use_cassette("cassettes/foo.yaml"):
    client.messages.create(...)
```

### pytest fixture (auto-named per test)
```python
def test_bar(promptecho_cassette):   # records to cassettes/test_bar.yaml
    client.messages.create(...)
```

The fixture defaults to `mode="once"` locally and `mode="none"` when `CI=true` — so a forgotten recording fails the build instead of making a live call. Configure it per test with the marker:

```python
@pytest.mark.promptecho(match_on=["model", "messages", "temperature"], mode="new_episodes")
def test_bar(promptecho_cassette): ...
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
@promptecho.use_cassette("cassettes/foo.yaml", mode="none")
```

Prompts changed and a pile of cassettes went stale? Re-record the whole suite without touching code — the env var overrides every cassette's mode:

```bash
PROMPTECHO_MODE=all pytest
```

### Choosing what to match on

Defaults to `["model", "messages", "system", "tools", "tool_choice", "reasoning_effort", "reasoning", "thinking"]` — everything that determines the response for a chat-shaped call, including reasoning-model knobs.

```python
@promptecho.use_cassette(
    "cassettes/foo.yaml",
    match_on=["model", "messages", "system", "temperature"],  # add temperature
)
```

For non-chat shapes (raw TGI `/generate`, embeddings) you'll want to override, e.g. `match_on=["model", "input"]` for an embeddings endpoint. See [SUPPORT.md → Request shapes](SUPPORT.md#request-shapes).

### Async

Works identically with `httpx.AsyncClient` and the async surfaces of Anthropic / OpenAI / Mistral SDKs — the async transport is patched the same way as sync.

---

## Cassette format

Human-readable YAML, designed to diff cleanly in PRs:

```yaml
version: 2
match_on: [model, messages, system, tools, tool_choice, reasoning_effort, reasoning, thinking]
interactions:
  - request:
      method: POST
      url: https://api.anthropic.com/v1/messages
      match_key: 7d206bed48a0bc0c        # fingerprint of method + URL path + matched fields
      matched_on: [model, messages, system, tools, tool_choice]
      body:                              # canonical (provider-normalized) body
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

- **Streamed** responses store the ordered SSE events under `response.events` with `streaming: true`; replay re-emits them in order.
- **Binary** responses (image/audio/octet-stream) get `binary: true` and the body is base64-encoded; replay decodes and returns the original bytes.
- **The stored body is the canonical, provider-normalized shape** — not the raw provider JSON. That makes cassettes provider-agnostic and easier to skim in code review.

Auto-redacted on record: the `authorization`, `x-api-key`, `openai-organization`, and `set-cookie` headers, plus **every URL query-string value** (query-param auth like `?key=…` never reaches disk). Configurable. Secrets *inside prompt text* are not auto-detected — don't put credentials in prompts.

See [`examples/cassettes/example.yaml`](examples/cassettes/example.yaml) for a real one.

---

## Status

**v0.1.0, working core. 19 tests, all green.** Not yet on PyPI.

Records and replays real httpx traffic — sync, async, SSE streaming, binary responses, cross-provider request shapes — verified end-to-end against a local server that gets shut down between record and replay.

### Roadmap (build-in-public)

Done:
- [x] httpx sync + async transport interception
- [x] SSE streaming record/replay
- [x] pytest plugin + auto-naming
- [x] Per-provider request normalizers (Anthropic / OpenAI / generic)
- [x] Reasoning-model match defaults (`reasoning_effort`, `thinking`, `reasoning`)
- [x] Binary response round-trip (image/audio/octet-stream — base64 in cassette)
- [x] Field-level diff on cassette miss (CI `mode=none` errors pinpoint the changed path, not just the field name)
- [x] `on_record_error` policy (`warn` / `raise` / `record`) — prevents silently baking transient 4xx/5xx into cassettes

Next:
- [ ] `requests` / `urllib3` interception backend — unlocks boto3-Bedrock and HF `InferenceClient`
- [ ] `promptecho lint` — find un-recorded calls in a test suite
- [ ] **`toMatchLLMSnapshot()` sibling** — semantic snapshot assertions on top of recorded calls

## FAQ

### Can I run cassettes concurrently?

One cassette at a time per process — promptecho patches httpx process-wide, and a nested or concurrent `use_cassette` raises `RuntimeError` immediately rather than interleaving recordings. `pytest-xdist` is fine (workers are separate processes). Note that while a cassette is active it intercepts **all** httpx traffic in the process, not just LLM calls.

## Design

For the why-not-the-other-way decisions — fingerprint vs raw bytes, why semantic matching is fenced off, how SSE re-emission works, how cross-provider normalization is structured — see [DESIGN.md](DESIGN.md).

## License

MIT
