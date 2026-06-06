# promptecho — design notes

The ergonomics — `use_cassette` decorator, record modes, pytest fixture — are
table stakes copied straight from vcrpy and aren't interesting. The six sections
below are where this tool earns its name. Each one is a deliberate choice and a
specific failure of the obvious "vcr-for-llms" approach.

## 1. Interception: at the HTTP transport, not the SDK

Both the Anthropic and OpenAI Python SDKs — and Mistral, Cohere v5+, `google-genai`,
the AnthropicBedrock / AnthropicVertex variants, and the OpenAI SDK pointed at
OpenRouter / Together / Fireworks / Cerebras / Groq / vLLM / TGI / SGLang via
`base_url=` — are all built on `httpx`. So we intercept once at `httpx`'s transport
layer and get every one of them for free, instead of monkeypatching each SDK's
`messages.create` / `chat.completions.create` surface (which would be a maintenance
treadmill as SDKs change).

The mechanism: [`patch.py`](src/promptecho/patch.py) monkeypatches
`httpx.HTTPTransport.handle_request` (sync) and `httpx.AsyncHTTPTransport.handle_async_request`
(async) for the duration of the `use_cassette` block, then restores the originals.
Each patched method routes the request through the record/replay decision and
either returns a fresh `httpx.Response` reconstructed from the cassette or passes
through to the real transport and captures the result.

The decision logic itself lives in [`transport.py`](src/promptecho/transport.py) and
is pure — no httpx, no I/O. That separation means the branching logic (cassette
miss → record? error? re-record?) is unit-testable without standing up a network
stack. It's the same separation `respx` and `vcrpy`'s httpx stub use.

**The cost of this choice:** SDKs not on httpx (boto3-Bedrock, HF `InferenceClient`,
`google-cloud-aiplatform`) are invisible — they just pass straight to the network as if
promptecho weren't installed. That's a deliberate v1 scope. The roadmap item
"`requests`/`urllib3` interception backend" closes it, at the cost of supporting two
transport stacks at once.

## 2. Matching: normalized fingerprint, not raw bytes

The crux, and the thing the obvious "vcr for LLMs" pitch gets wrong.

For **replay** you want determinism: the same logical request must map to the same
recording, every time. Raw-byte matching (vcrpy default) fails because LLM bodies
carry volatile noise — client-injected request IDs, reordered `tools` arrays,
key-order and whitespace differences from re-serialization. Same call, different
bytes, missed match.

So we compute a **fingerprint** over only the fields that determine the response:

```
fingerprint(body) = sha256( canonical_json( pick(body, match_on) ) )
```

- `canonical_json` sorts keys and strips insignificant whitespace, so
  re-serialization can't change the key.
- Volatile fields are simply not in `match_on`, so they can't affect the match.

The default `match_on` is:
```python
["model", "messages", "system", "tools", "tool_choice",
 "reasoning_effort", "reasoning", "thinking"]
```

`model`, `messages`, `system`, `tools`, `tool_choice` are obvious. The last three
are less obvious and matter: reasoning-model knobs (OpenAI `reasoning_effort`,
Anthropic `thinking`, OpenRouter `reasoning`) change the response without changing
the prompt. If they aren't in the default match set, a test with
`reasoning_effort="high"` would silently replay the recording made for
`"low"` — a wrong-fixture bug that's hard to catch by eye. So they're in by
default, even though omitting them would yield "smaller" fingerprints. Correctness
wins.

`match_on` is also user-configurable, because only the test author knows which
fields are load-bearing for *their* assertion (does this test care about
`temperature`? `max_tokens`?).

**What we deliberately do NOT do:** semantic / embedding matching on replay.
"Different prompt, embedding-close enough → same recording" reintroduces
non-determinism into the exact thing you adopted promptecho to make deterministic,
and can silently serve the wrong recording. Semantic matching is a *caching*
concern, not a *testing* one. Keeping these two ideas separate is a core stance
— see the README's "What promptecho is not." A `fuzzy=True` dev-loop convenience
is on the roadmap; it will never be the default and never used in CI.

## 3. Cross-provider canonicalization

The same logical prompt is expressed in different wire shapes across providers,
SDK versions, and even within a single provider's API:

- Anthropic puts the system prompt in a top-level `system` param; OpenAI puts
  it in a `system`- or `developer`-role message.
- Message content may be a bare string or a list of typed content blocks.
- Tool defs differ: Anthropic `{name, description, input_schema}` vs OpenAI
  `{type: function, function: {name, description, parameters}}`.
- OpenAI's `max_completion_tokens` is an alias of `max_tokens` for newer models.

[`normalizers.py`](src/promptecho/normalizers.py) maps each raw provider body into
one canonical shape *before* fingerprinting. That's the capability a raw-bytes HTTP
VCR fundamentally cannot have — and the reason "just point vcrpy at httpx" is
unsatisfying. Provider is detected by URL host first, body-shape fallback (so
localhost / self-hosted gateways / private proxies behave the same as the
brand-name hosts).

Two consequences worth being explicit about:

1. **The canonical body is what's stored on disk** — not the raw provider JSON.
   This makes cassettes provider-agnostic and easier to skim in code review, at
   the cost of being one step removed from the wire format. Worth it.
2. **Lossy joins.** Where shapes don't map cleanly, we choose the simpler form:
   e.g. a multi-block system prompt collapses to a `\n`-joined string. That's
   fine for matching; if you have something exotic worth preserving, override
   `match_on` to include the original field path instead.

Open: more providers (Gemini, Mistral) get the same treatment, and a
user-pluggable normalizer hook for in-house gateways with custom shapes.

## 4. Streaming: record the events, re-emit faithfully

Most real LLM calls are `stream=True` (SSE). A recording that only captures the
final assembled body is useless for testing streaming code paths — you can't
test progressive UI rendering, token-budget cutoffs, or streaming-tool-call
handling against a one-shot fixture.

promptecho captures the **ordered list of SSE events** as they arrive, stores them
under `response.events`, and on replay re-emits them as a synthetic stream — so
`for chunk in stream:` iterates identically against the cassette, including event
boundaries and `message_delta` / `content_block_delta` / reasoning-delta ordering.

This is fiddly (chunked transfer, `[DONE]` sentinels, provider-specific event
shapes) and is exactly why generic HTTP VCR tools are unsatisfying for LLM work.
Getting it right is the moat.

**Known limitation:** on the *recording* run we buffer the full upstream
response before returning it, so you lose true streaming timing while recording.
Replay streams fine. Acceptable for a test tool — the recording run isn't where
you measure latency.

## 5. Binary responses: detect, base64, round-trip

Image-out, audio-out, and any `application/octet-stream` body cannot be
text-decoded — the bytes have to survive the cassette round-trip exactly. A
YAML cassette that runs binary bodies through `.decode("utf-8", "replace")`
silently corrupts them (this was a real bug found by probe-testing before launch).

So `_capture()` inspects `Content-Type` and, for anything `image/*`, `audio/*`,
`video/*`, `application/octet-stream`, `pdf`, or `zip`, stores the body
base64-encoded with a `binary: true` flag. Replay decodes it back. **Verified
byte-equal** end-to-end (record → server shutdown → replay) in
`tests/test_reasoning_and_binary.py::test_binary_image_response_byte_exact`.

Multimodal-as-JSON (base64 inside `content` blocks — the Anthropic / OpenAI
vision / GPT-4o image-out shape) was already fine, because the base64 string
lives inside JSON and never gets text-decoded as bytes. That stays covered by
its own test.

`multipart/form-data` (file uploads/downloads) is explicitly out of scope for
v1 — large payloads, encoding edge cases, rarely a thing you want to freeze in a
fixture.

## 6. Secrets: redact on record

A cassette is meant to be committed, so it must be safe by default — opt-out, not
opt-in. On record we strip `authorization`, `x-api-key`, and `openai-organization`
headers and never write request auth to disk. The list is configurable
(`REDACT_HEADERS` in [`cassette.py`](src/promptecho/cassette.py)) — extend it for
provider-specific auth headers, never shrink it without thinking carefully.

Body-level secrets (a prompt that happens to contain a credential) are *not*
auto-redacted, because there's no reliable way to detect them. The escape hatch
is don't put secrets in prompts — but a future `redact_body=[...]` hook is a
reasonable addition.

---

## Open design threads (good build-in-public material)

- **Field-level diff on cassette miss in CI.** On a `mode=none` miss, show the
  field-level difference between the incoming request and the nearest recorded
  one — "you changed `messages[1].content`" — so the failure is actionable rather
  than "no match." Already scaffolded by `diff_fields()` in `matcher.py`; needs
  better surfacing through the patch layer.
- **Drift detection.** An optional `mode=all` run in a nightly (not PR) CI job
  that re-records and flags when a model's output to a frozen prompt has changed
  — turning cassettes into a cheap model-regression tripwire. The hardest part
  is choosing the "did anything meaningful change?" comparator (raw text diff is
  noisy; LLM-judge re-introduces non-determinism). Reasonable defaults: structural
  diff for tool-call shapes, text-similarity for prose.
- **A second interception backend (`requests`/`urllib3`).** Unlocks boto3-Bedrock
  and HF `InferenceClient`. Non-trivial: different stack, no clean
  `BaseTransport` equivalent in urllib3, and Bedrock specifically signs requests
  at the urllib3 level with SigV4 — so any fingerprint over the request would
  pick up timestamp/signature noise unless we strip them first. Worth doing when
  there's evidence of demand, not before.
- **User-pluggable normalizers.** For in-house gateways with custom shapes. A
  `register_normalizer(detect_fn, normalize_fn)` API is straightforward; the
  open question is whether to ship it before or after the surface stabilizes.
