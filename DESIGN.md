# tapedeck — design notes

This is the technical core. The ergonomics (decorator, modes) are table stakes copied
from vcrpy; the three things below are where this tool earns its name.

## 1. Interception: at the HTTP transport, not the SDK

Both the Anthropic and OpenAI Python SDKs (and most others) are built on `httpx`. So we
intercept once, at `httpx`'s transport layer, and get every provider for free — instead of
monkeypatching each SDK's `messages.create` / `chat.completions.create` surface, which would
be a maintenance treadmill as SDKs change.

`use_cassette` activates a patch that swaps the real `httpx` transport for `TapedeckTransport`,
which sits in front of the real one and decides — per request — whether to **replay** a recorded
response or **pass through and record**. This is the same mechanism `respx` and `vcrpy`'s httpx
stub use. Async (`handle_async_request`) is implemented with the same logic.

The decision logic lives in [`transport.py`](src/tapedeck/transport.py); it's pure and unit-testable
in isolation from the patching mechanics.

## 2. Matching: normalized fingerprint, not raw bytes

The crux, and the thing the obvious "vcr for llms" pitch gets wrong.

For **replay** you want determinism: the same logical request must map to the same recording,
every time. Raw-byte matching (vcrpy default) fails because LLM bodies carry volatile noise —
client-injected request IDs, reordered `tools` arrays, key-order and whitespace differences from
re-serialization. Same call, different bytes, missed match.

So we compute a **fingerprint** over only the fields that determine the response:

```
fingerprint(body) = sha256( canonical_json( pick(body, match_on) ) )
```

- `match_on` defaults to `["model", "messages", "system", "tools", "tool_choice"]`.
- `canonical_json` sorts keys and strips insignificant whitespace, so re-serialization can't change the key.
- Volatile fields are simply not in `match_on`, so they can't affect the match.

`match_on` is user-configurable because only the test author knows which fields are load-bearing
for *their* assertion (does this test care about `temperature`? `max_tokens`?).

**What we deliberately do NOT do:** semantic / embedding matching on replay. "Different prompt,
embedding-close enough → same recording" reintroduces non-determinism into the exact thing you
adopted tapedeck to make deterministic, and can silently serve the wrong recording. Semantic
matching is a *caching* concern, not a *testing* one. It exists only as an explicit opt-in
`fuzzy=True` dev-loop convenience, never in CI, never default. Keeping these two ideas separate
is a core design stance — see README "What tapedeck is not."

## 3. Streaming: record the events, re-emit faithfully

Most real LLM calls are `stream=True` (SSE). A recording that only captures the final assembled
body is useless for testing streaming code paths. tapedeck captures the **ordered list of SSE
events** as they arrive, stores them under `response.events`, and on replay re-emits them as a
synthetic stream — so `for chunk in stream:` iterates identically against the cassette, including
event boundaries and `message_delta` / `content_block_delta` ordering.

This is fiddly (chunked transfer, `[DONE]` sentinels, provider-specific event shapes) and is
exactly why generic HTTP VCR tools are unsatisfying for LLM work. Getting it right is the moat.

## 4. Secrets: redact on record

On record we strip `authorization`, `x-api-key`, and `openai-organization` headers (configurable
allow/deny list) and never write request auth to disk. A cassette is meant to be committed, so it
must be safe by default — opt-out, not opt-in.

## Open design questions (good build-in-public threads)

- ~~**Match-on defaults per provider.**~~ **Done** — `normalizers.py` maps each provider to a
  canonical request shape *before* fingerprinting (Anthropic top-level `system` ≡ OpenAI
  `system`-role message; string content ≡ single text block; tool-def shapes unified). The
  canonical body is what gets stored, so cassettes are provider-agnostic and readable. Detection is
  URL-host first, body-shape fallback. Open extension: more providers (Gemini, Mistral) and a
  user-pluggable normalizer hook.
- **Partial-match diagnostics.** On a cassette miss in `mode=none`, show a field-level diff between
  the incoming request and the nearest recorded one — "you changed `messages[1].content`" — so the
  failure is actionable, not just "no match."
- **Drift detection.** Optional `mode=all` run in a nightly (not PR) CI job that re-records and
  flags when a model's output to a frozen prompt has changed — turning cassettes into a cheap
  model-regression tripwire.
