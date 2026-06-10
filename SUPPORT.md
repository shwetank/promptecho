# Support matrix

What promptecho does and doesn't capture today. **This file is the source of truth.**
A row marked ✅ is exercised by a test in `tests/`; ❌ is a known limit with the
workaround (if any) inline.

**One rule explains everything below:** promptecho intercepts at the `httpx`
transport layer. If the client uses httpx, promptecho sees the call. If it uses
`requests`, `urllib3`, `aiohttp`, or `grpc`, it doesn't. There is no silent
degradation — an unsupported transport just goes to the network as if promptecho
weren't installed.

---

## Providers and SDKs

### ✅ Covered

| How you call the model | Why it works |
|---|---|
| **Anthropic Python SDK** (`anthropic`, including `AnthropicBedrock` and `AnthropicVertex`) | All on httpx |
| **OpenAI Python SDK** (`openai`) | On httpx |
| **OpenAI SDK with custom `base_url`** → OpenRouter, Together, Fireworks, Cerebras, Groq, DeepInfra, Perplexity | OpenAI SDK is on httpx; body-shape detection fills in for unknown hosts |
| **Self-hosted OpenAI-compatible servers** — vLLM, TGI, SGLang, LM Studio, Ollama | Same path: OpenAI SDK + `base_url=http://localhost:…` |
| **Your own fine-tune** served behind any of the above | Inherits coverage from the gateway |
| Mistral SDK, Cohere v5+, `google-genai` | All on httpx |
| Raw `httpx.Client` / `httpx.AsyncClient` | Useful for in-house gateways with custom shapes — override `match_on` |

### ⚠️ Partial

| How you call the model | Caveat |
|---|---|
| **LiteLLM** | Most provider paths route through httpx and work; a few legacy paths use `requests`. File an issue if a specific provider misses. |

### ❌ Not covered (with workarounds)

| How you call the model | Why | Workaround |
|---|---|---|
| **Amazon Bedrock via `boto3` / `bedrock-runtime`** | boto3 uses urllib3 + SigV4 signing, not httpx | Use `AnthropicBedrock` (Claude on Bedrock, httpx-based — ✅), or call Bedrock's HTTP endpoint through `httpx` directly |
| **Hugging Face `huggingface_hub.InferenceClient`** | Uses `requests` / `aiohttp` | Many HF endpoints also expose an OpenAI-compatible URL — point the OpenAI SDK at it |
| **Google Vertex via `google-cloud-aiplatform`** | grpc / requests, not httpx | Use `google-genai` (httpx) or `AnthropicVertex` (httpx) |

### ⛔ Out of scope

| Setup | Why |
|---|---|
| In-process models (`transformers`, `llama-cpp-python`, in-proc vLLM) | No HTTP call to freeze. Use a normal mocking pattern. |

---

## Request shapes

| Shape | Status | Notes |
|---|---|---|
| Chat completions / Messages (Anthropic, OpenAI) | ✅ | Default `match_on` is built for this |
| **Reasoning models** — o1/o3, Claude extended thinking, OpenRouter `reasoning`, DeepSeek-R1 | ✅ | `reasoning_effort`, `reasoning`, `thinking` are in `DEFAULT_MATCH_ON` — high/low don't collide |
| Tool use / function calling | ✅ | Tool defs canonicalized cross-provider (Anthropic `input_schema` ≡ OpenAI `function.parameters`) |
| System prompts | ✅ | Anthropic top-level `system` ≡ OpenAI `system`-role message ≡ OpenAI `developer` role |
| Native TGI `/generate`, custom non-chat bodies | ⚠️ | Default `match_on` won't see the body — pass `match_on=["inputs","parameters"]` |
| Embeddings (`/v1/embeddings`) | ⚠️ | Captured; override match: `match_on=["model","input","encoding_format"]` |
| Audio transcription (`/v1/audio/transcriptions`) | ⚠️ | Uses `multipart/form-data` for the audio upload — see below |
| **Bedrock model id in URL path** (not the body) | ✅ | The URL path is part of the fingerprint (cassette format v2), so calls to different models can't collide even when `model` is absent from the body |

---

## Response shapes

| Shape | Status | Notes |
|---|---|---|
| JSON responses (the common case) | ✅ | Round-trips via YAML |
| **SSE streaming** — Anthropic `stream=True`, OpenAI chat stream, reasoning deltas | ✅ | Events captured in order, re-emitted byte-for-byte on replay |
| **Multimodal as JSON** — base64 image/audio inside content blocks (Claude image-in/out, OpenAI vision, GPT-4o image-out) | ✅ | The base64 string lives in the JSON; round-trip is byte-exact |
| **Raw binary** — `image/*`, `audio/*`, `video/*`, `application/octet-stream`, `pdf`, `zip` | ✅ | Detected by `Content-Type`, base64-encoded in the cassette; replay decodes back. Verified byte-equal in `tests/test_reasoning_and_binary.py::test_binary_image_response_byte_exact` |
| Encrypted reasoning blobs (OpenAI `reasoning.encrypted_content`) | ✅ | Opaque field, round-trips verbatim in the JSON body |
| `multipart/form-data` (file uploads/downloads, audio transcription requests) | ⚠️ | Recorded and replayed; the request is matched by an exact hash of its raw bytes (cassette format v2), so any byte change (including multipart boundary strings, which some clients randomize per request) is a miss. Fine for stable payloads; no field-level matching or diffs. |

---

## Concurrency

promptecho patches httpx **process-wide**: while a cassette is active, every
httpx call in the process — from any thread or event loop, LLM or not — routes
through it. Consequences:

- **One cassette at a time per process.** A nested or concurrent
  `use_cassette` raises `RuntimeError` immediately rather than silently
  interleaving recordings into the wrong cassette.
- **`pytest-xdist` works** — its workers are separate processes, each with its
  own patch.
- Background threads making non-LLM httpx calls during an active cassette get
  recorded/replayed too. Keep unrelated traffic out of cassette blocks.

---

## Reporting a gap

- **Call is on httpx but isn't being captured?** That's a bug. File an issue with
  the SDK + version and a minimal repro.
- **Your client uses `requests` / `aiohttp` / `urllib3`?** That's a known v1 gap.
  Tell us which SDK and use case — it's how the second-backend roadmap item gets
  prioritized.
- **Cassette miss in `mode=none` that you don't understand?** The error message
  includes a field-level diff against the most similar recording (exact path,
  recorded vs incoming value). If the diff doesn't explain it, file an issue
  with the message — that's a diagnostics bug we want to fix.
