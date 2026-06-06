# Support matrix

What tapedeck does and does not capture today. This file is the source of truth —
if a row says ✅ it's covered by a test in `tests/`; if ❌, it's a known limit
and the workaround (if any) is documented.

The single rule behind everything: **tapedeck intercepts at the `httpx`
transport layer.** If the client uses httpx, tapedeck can see the call. If it
uses `requests`, `urllib3`, or `aiohttp`, it cannot. There is no silent
degradation — an unsupported transport just goes straight to the network as if
tapedeck weren't installed.

## Providers and SDKs

| How you call the model | Transport | Status | Notes |
|---|---|---|---|
| Anthropic Python SDK (`anthropic`) | httpx | ✅ | Direct API; also AnthropicBedrock / AnthropicVertex use httpx → covered |
| OpenAI Python SDK (`openai`) | httpx | ✅ | Direct API |
| OpenAI SDK with custom `base_url` → **OpenRouter, Together, Fireworks, Cerebras, Groq, DeepInfra, Perplexity, etc.** | httpx | ✅ | This is the canonical pattern for hosted open-source / fine-tuned models |
| OpenAI SDK → **self-hosted vLLM / TGI / SGLang / LM Studio / Ollama** (OpenAI-compatible mode) | httpx | ✅ | Detection falls back to body shape when host is unknown |
| Mistral SDK, Cohere v5+, `google-genai` | httpx | ✅ | All on httpx |
| Raw `httpx.Client` / `httpx.AsyncClient` | httpx | ✅ | Useful for unusual or in-house gateways |
| LiteLLM | mostly httpx | ⚠️ | Most provider paths route through httpx and work; a few legacy paths use `requests`. File an issue if you hit one. |
| **Amazon Bedrock via `boto3` / `bedrock-runtime`** | urllib3 (+ SigV4) | ❌ | boto3 doesn't use httpx. **Workaround:** use Anthropic/Cohere SDKs in Bedrock mode (they're on httpx and *are* covered), or call Bedrock's HTTP endpoint through `httpx` directly. |
| **Hugging Face `huggingface_hub.InferenceClient`** | requests / aiohttp | ❌ | **Workaround:** their newer `text-generation` endpoints often expose an OpenAI-compatible URL — point the OpenAI SDK at it. |
| **Google Vertex via `google-cloud-aiplatform`** | grpc / requests | ❌ | **Workaround:** use `google-genai` (httpx) or `AnthropicVertex` (httpx). |
| In-process models (`transformers`, `llama-cpp-python`, in-proc vLLM) | none | ⛔ | No HTTP call to freeze; out of scope. Use a normal mocking pattern. |

## Request shapes

| Shape | Status | Notes |
|---|---|---|
| Chat completions / Messages (Anthropic and OpenAI) | ✅ | The default `match_on` is built for this |
| **Reasoning models** (o1/o3, Claude extended thinking, OpenRouter `reasoning`, DeepSeek-R1) | ✅ | `reasoning_effort`, `reasoning`, `thinking` are in `DEFAULT_MATCH_ON` — high/low don't collide |
| Tool use / function calling | ✅ | Tool defs canonicalized cross-provider (Anthropic `input_schema` ≡ OpenAI `function.parameters`) |
| System prompts | ✅ | Anthropic top-level `system` ≡ OpenAI `system`-role message ≡ OpenAI `developer` role |
| Native TGI `/generate`, custom non-chat bodies | ⚠️ | Default `match_on` won't see the body — pass `match_on=["inputs","parameters"]` (or your own field list) |
| Embeddings (`/v1/embeddings`) | ⚠️ | Captured; `match_on` defaults don't include `input` — pass `match_on=["model","input","encoding_format"]` |
| **Bedrock model id in URL path** (not the body) | ⚠️ | Even if you intercept the httpx call, `model` falls out of the fingerprint — split cassettes per model |

## Response shapes

| Shape | Status | Notes |
|---|---|---|
| JSON responses (the common case) | ✅ | Round-trip via YAML |
| SSE streaming (Anthropic `messages.create(stream=True)`, OpenAI chat stream, reasoning deltas) | ✅ | Events captured in order, re-emitted byte-for-byte on replay |
| **Multimodal as JSON** (base64 image/audio inside content blocks — Claude image-in/out, OpenAI vision, GPT-4o image-out) | ✅ | The base64 string lives in the JSON; round-trip is byte-exact |
| **Raw binary responses** (`image/*`, `audio/*`, `video/*`, `application/octet-stream`, `pdf`, `zip`) | ✅ | Detected by `Content-Type` and base64-encoded in the cassette; replay decodes back. **Verified byte-equal** in `tests/test_reasoning_and_binary.py::test_binary_image_response_byte_exact` |
| Encrypted reasoning blobs (OpenAI `reasoning.encrypted_content`) | ✅ | Opaque field, round-trips verbatim in the JSON body |
| `multipart/form-data` (file uploads/downloads) | ❌ | Not supported in v1 — large payloads, encoding edge cases. Avoid recording these. |

## "Will this work for my stack?" cheat sheet

- **OpenAI / Anthropic / Mistral / Cohere SDK?** Yes.
- **Hosted open-source via OpenRouter / Together / Fireworks / Cerebras / Groq?** Yes — they're OpenAI-compatible, you're using the OpenAI SDK with `base_url=`, that's httpx.
- **Your own fine-tune served behind an OpenAI-compatible URL (vLLM, TGI, SGLang)?** Yes.
- **Bedrock through boto3?** No, but use `AnthropicBedrock` if your model is Claude.
- **HF `InferenceClient`?** No — but if the model also has an OpenAI-compatible endpoint, use that.
- **`transformers` calling the model in-process?** Nothing to record. Out of scope.

## Reporting a gap

If you hit a real call that's on httpx but doesn't capture, that's a bug — file
an issue with the SDK and a minimal repro. If your client uses `requests` or
`aiohttp`, that's a known gap; tell us which SDK so it can be tracked as
demand for a second interception backend.
