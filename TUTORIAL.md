# tapelog tutorial

Practical recipes for using tapelog in real codebases — from a one-engineer
prototype, to a tight startup, to a 200-engineer enterprise codebase, to a
research project that needs to be reproducible six months from now.

The first five minutes are universal. After that, pick the scenario chapter
closest to yours and skim the rest as needed.

- [Part 1: Your first five minutes](#part-1-your-first-five-minutes)
- [Part 2: Pick your scenario](#part-2-pick-your-scenario)
  - [2a. Startup — shipping LLM features fast](#2a-startup--shipping-llm-features-fast)
  - [2b. Enterprise — hardening tests for production](#2b-enterprise--hardening-tests-for-production)
  - [2c. Research — reproducible experiments](#2c-research--reproducible-experiments)
- [Part 3: Patterns everyone needs eventually](#part-3-patterns-everyone-needs-eventually)
  - [Re-recording when your prompt changes](#re-recording-when-your-prompt-changes)
  - [Debugging a cassette miss](#debugging-a-cassette-miss)
  - [Tuning `match_on` for your test](#tuning-match_on-for-your-test)
  - [Switching providers without changing test code](#switching-providers-without-changing-test-code)
  - [Reasoning models](#reasoning-models)
  - [Multimodal: images, audio, vision](#multimodal-images-audio-vision)
- [Part 4: Anti-patterns](#part-4-anti-patterns)

---

## Part 1: Your first five minutes

Install:
```bash
pip install -e path/to/tapelog   # not yet on PyPI
```

Write a test the way you normally would:
```python
# test_summarize.py
import tapelog
from anthropic import Anthropic

@tapelog.use_cassette("cassettes/summarize.yaml")
def test_summarize():
    msg = Anthropic().messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[{"role": "user", "content": "Summarize: the cat sat on the mat."}],
    )
    assert "cat" in msg.content[0].text.lower()
```

Run it the **first** time:
```bash
ANTHROPIC_API_KEY=sk-... pytest test_summarize.py
```
→ one real API call, recorded to `cassettes/summarize.yaml`.

Run it every time **after** that:
```bash
pytest test_summarize.py   # no API key needed
```
→ replayed from disk. Zero network, zero tokens, deterministic.

Commit the cassette. The recording is the test fixture — your CI doesn't make
live calls and your laptop doesn't either.

That's it. The rest of this doc is variations.

---

## Part 2: Pick your scenario

### 2a. Startup — shipping LLM features fast

The pain: you're iterating on prompts daily, sometimes hourly. A test suite that
locks in last week's prompt response is friction, not safety. You need the
testing benefit (catch real regressions) without paying tax for every prompt
tweak.

**Pattern: cheap re-recording on prompt change.**

```python
# Use the auto-named fixture so each test owns its own cassette file.
def test_extracts_invoice_total(tapelog_cassette):
    # records to cassettes/test_extracts_invoice_total.yaml the first time
    extracted = extract_invoice(SAMPLE_PDF_TEXT)
    assert extracted["total"] == 1247.50
```

When you change the prompt that `extract_invoice` uses:
```bash
# delete the cassette, re-run once, commit the fresh fixture
rm cassettes/test_extracts_invoice_total.yaml
pytest tests/test_extract.py::test_extracts_invoice_total
git add cassettes/test_extracts_invoice_total.yaml
git commit
```

Or, for a sweep, set the mode globally for one run:
```bash
TAPELOG_MODE=all pytest tests/test_extract.py   # re-records everything
```
(Add this env-var override in your `conftest.py` — it's a 6-line fixture; see
[example below](#tip-rerecord-env-var).)

**Trade-off you're choosing.** You're prioritizing iteration speed over change
review. The cassette diff in your PR *is* your review surface: if the new
recording looks bad, reject the PR. That's lighter than full evals, and the
right weight for a startup that hasn't earned eval infrastructure yet.

#### Provider switching during the build

Startup move: you start on OpenAI, switch to OpenRouter for cost, then to a
self-hosted vLLM behind an OpenAI-compat endpoint when you raise. Your test
code doesn't have to change:

```python
@pytest.fixture
def llm():
    return OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ["LLM_API_KEY"],
    )

def test_summarize(llm, tapelog_cassette):
    r = llm.chat.completions.create(model=MODEL, messages=[...])
    assert r.choices[0].message.content
```

Same test runs against OpenAI, OpenRouter (Llama-3.1-70B), and `localhost:8000`
vLLM. Different cassettes per environment if you want strict matching; one
cassette if the model output is similar enough that you're matching on
structural correctness, not exact text.

#### Demo determinism

If you're recording a sales/investor demo, run the demo path once against the
live API, commit the cassette, demo from replay. Your demo can't flake, can't
hallucinate live, and runs offline on stage.

```python
@tapelog.use_cassette("cassettes/demo_walkthrough.yaml", mode="none")
def demo_script():
    # everything here replays from the recording — deterministic
    ...
```

`mode="none"` ensures that if the cassette is somehow missing on the demo
laptop, you get a loud error during rehearsal — not a surprise live call on
stage.

---

### 2b. Enterprise — hardening tests for production

The pain: large codebase, many teams, real budgets, real compliance review,
real incident-response stakes. A flaky LLM test is annoying for a startup; in
an enterprise it can hide a regression that ships to a million users.

**Pattern: hard-fail CI on any live call.**

```python
# conftest.py at repo root
import os, pytest, tapelog

@pytest.fixture(autouse=True)
def _ci_mode_none(tapelog_cassette):
    """In CI, every test that uses the tapelog fixture must replay, never
    record. A cassette miss fails the build instead of making a live call."""
    yield  # fixture already enforces this when CI=true
```

The shipped `tapelog_cassette` fixture already defaults to `mode="none"` when
`CI=true` in the environment. The reason to wire it explicitly in your
`conftest.py` is to make the intent grep-able for a security or platform team
reviewing why the CI box doesn't have API keys.

**Pattern: prevent accidental live calls in *any* test, not just LLM ones.**

```python
# conftest.py — for the truly paranoid
import socket, pytest

@pytest.fixture(autouse=True)
def _no_network_in_ci(monkeypatch):
    if os.environ.get("CI"):
        def blocked(*a, **kw):
            raise RuntimeError("No live network calls allowed in CI — use a cassette.")
        monkeypatch.setattr(socket, "socket", blocked)
```

This isn't tapelog-specific but pairs with it well: any test that doesn't have
a cassette and tries to actually hit the network gets caught at the socket
layer.

#### PR review as a quality gate

Cassettes are YAML. They diff cleanly in PR review. Train your reviewers:

- A new cassette file means a new prompt or new call site. Read the recorded
  request body. Is the prompt OK? Does it leak data?
- A *modified* cassette means the prompt or model changed. Read the diff. Did
  the response materially change? Should this be one PR or two (prompt change +
  response approval)?

This is cheap and is one of the most under-priced wins of the whole pattern:
your PR review tool becomes your prompt-change review tool, for free.

#### Compliance and PII

Header secrets (`authorization`, `x-api-key`, `openai-organization`) are
redacted by default — see `REDACT_HEADERS` in `cassette.py`. Body secrets
(PII inside a prompt) are not auto-redacted because there's no reliable
detector. Three patterns:

1. **Don't put PII in prompts.** This is the answer for ~95% of cases.
2. **Use synthetic test data.** Generate fake invoices/medical-notes/whatever
   for your fixtures. Cassettes are committed; the data in them is forever.
3. **Post-process before commit** if you must record against real data:
   ```python
   # scripts/scrub_cassettes.py — run as a pre-commit hook
   for cassette in glob("cassettes/**/*.yaml"):
       data = yaml.safe_load(open(cassette))
       redact_pii_in_place(data)            # your domain-specific scrubber
       yaml.safe_dump(data, open(cassette, "w"))
   ```

A future `redact_body=[...]` API will make pattern 3 a one-liner; for now it's
a hook you write.

#### Multi-team conventions

If many teams share a repo, agree on:

- **Cassette location** — `cassettes/` next to tests (the fixture default), or
  a central `fixtures/cassettes/` directory. Default is fine; pick one and
  document.
- **`match_on` defaults** — the shipped default is right for ~all chat-shaped
  tests. Document exceptions (embeddings, raw `/generate`, internal gateway
  shapes) in your team's testing README.
- **Re-record policy** — who can re-record against the live API, and when. A
  common rule: re-record is a separate PR from the code change that motivated
  it, so a reviewer sees the response change in isolation.

#### Bedrock-via-boto3 workaround

If your enterprise is AWS-mandated and your code uses
`boto3.client("bedrock-runtime")`, that path uses urllib3 + SigV4 and isn't
covered (see [SUPPORT.md](SUPPORT.md)). The pragmatic move is to migrate the
LLM call sites to `AnthropicBedrock`:

```python
# before — not covered
import boto3
client = boto3.client("bedrock-runtime")
r = client.invoke_model(modelId="anthropic.claude-...", body=...)

# after — covered, same auth (Bedrock IAM)
from anthropic import AnthropicBedrock
client = AnthropicBedrock(aws_region="us-east-1")  # uses IAM
r = client.messages.create(model="anthropic.claude-...", messages=[...])
```

Same auth, same model, same backend — but now httpx-based and tapelog sees it.

---

### 2c. Research — reproducible experiments

The pain: six months after a paper or experiment, you (or a reviewer) need to
re-run a numbered cell and get the same answer. The underlying model has been
deprecated or its weights have shifted. The result is unreproducible — and the
discipline of "save the raw outputs" usually decays fast.

**Pattern: one cassette per experiment, shipped alongside the code.**

```
experiment-2026-claude-vs-gpt/
├── README.md
├── run.py
├── cassettes/
│   ├── claude_opus_baseline.yaml
│   ├── gpt5_baseline.yaml
│   └── claude_opus_cot_v2.yaml
└── results/
    └── 2026-04-15.csv
```

```python
# run.py
@tapelog.use_cassette(f"cassettes/{CONDITION}.yaml")
def run_condition(prompts):
    return [model_call(p) for p in prompts]
```

Anyone who clones the repo six months later, with no API key, can re-run
`run.py` and get the exact outputs that produced the paper's tables.

#### Comparing providers offline

Record each provider once, then iterate on your analysis with no further API
spend:

```python
def record_baselines():
    for provider in ["openai", "anthropic", "openrouter_llama"]:
        with tapelog.use_cassette(f"cassettes/{provider}.yaml", mode="once"):
            for prompt in BENCHMARK_PROMPTS:
                CLIENTS[provider].chat.completions.create(...)

def analyze():
    # zero cost, zero variance, runs offline
    for provider in ["openai", "anthropic", "openrouter_llama"]:
        with tapelog.use_cassette(f"cassettes/{provider}.yaml", mode="none"):
            for prompt in BENCHMARK_PROMPTS:
                response = CLIENTS[provider].chat.completions.create(...)
                yield score(response)
```

The cassette dir becomes the input to your analysis. You can re-run a notebook
twenty times tuning the scorer without paying twenty times.

#### Long-running notebooks

```python
# top of the notebook
import tapelog
ctx = tapelog.use_cassette("cassettes/notebook_session.yaml", mode="once")
ctx.__enter__()   # leave open for the rest of the session
```

Now re-executing any cell that calls the LLM hits the cassette instead of the
API. New prompts get appended on first run; identical prompts replay. When you
restart the kernel, the cassette is still there.

This is roughly the same idea as `joblib.Memory` for sklearn — disk-backed
memoization — but at the HTTP layer so it works across any LLM client.

#### Reproducibility hygiene

- Commit cassettes. They're text. Git handles them fine. (For very large
  cassettes, Git LFS is reasonable.)
- Include `cassettes/` in the artifact you upload to OSF / Zenodo / your
  paper's supplemental.
- Record the **model version** in the cassette filename or directory (the
  cassette body has it too, but filename makes it obvious): `claude-opus-4-8/`,
  `gpt5/`, etc.

---

## Part 3: Patterns everyone needs eventually

### Re-recording when your prompt changes

A prompt change *should* produce a new recording, because the fingerprint of
the request changes. The three modes that matter:

| Situation | Mode | Effect |
|---|---|---|
| You changed a prompt and want a fresh recording | delete the cassette, run `mode="once"` (default) | One real call, recorded |
| You changed many prompts | `mode="all"` for one run | Re-records every interaction, ignoring existing cassettes |
| You want to *add* to the cassette without disturbing existing ones | `mode="new_episodes"` | Existing recordings replay; new requests get recorded and appended |

<a id="tip-rerecord-env-var"></a>
**Tip: env-var-controlled mode.** A 6-line conftest fixture so you can toggle
mode without editing decorators:

```python
# conftest.py
import os, pytest, tapelog

@pytest.fixture
def tapelog_cassette(request, tmp_path_factory):
    cassette_dir = os.path.join(os.path.dirname(request.fspath), "cassettes")
    os.makedirs(cassette_dir, exist_ok=True)
    path = os.path.join(cassette_dir, f"{request.node.name}.yaml")
    mode = os.environ.get("TAPELOG_MODE", "none" if os.environ.get("CI") else "once")
    with tapelog.use_cassette(path, mode=mode) as c:
        yield c
```

Then: `TAPELOG_MODE=all pytest tests/test_thing.py` re-records that file.

### Debugging a cassette miss

You changed something tiny and now `mode="none"` fails with "no matching
recording." How to figure out what changed:

1. **Temporarily switch to `mode="once"`** (or delete the cassette) and re-run
   the test. You get a new recording.
2. **Diff the new cassette against the old one** in git. The `body:` block under
   `request:` is the canonical request shape — whatever's different there is
   what changed the fingerprint.
3. **If the change was intentional** (you genuinely changed the prompt), commit
   the new cassette.
4. **If the change was unintentional** (you accidentally changed
   `temperature`, or your conftest is leaking a different `max_tokens`), fix
   the code so the request matches the old fingerprint, then revert the
   cassette.

A future "field-level diff on miss" feature will print exactly what changed
without this dance. For now, `git diff` on the cassettes is the tool.

### Tuning `match_on` for your test

Default `match_on`:
```python
["model", "messages", "system", "tools", "tool_choice",
 "reasoning_effort", "reasoning", "thinking"]
```

Override when you're testing something specific:

- **Testing temperature behavior:** add `temperature` so the test that uses
  `temperature=0` doesn't accidentally replay a recording made at `0.7`.
- **Testing token-budget behavior:** add `max_tokens`.
- **Testing a non-chat shape** (raw TGI `/generate`, embeddings):
  `match_on=["model", "inputs", "parameters"]` or
  `match_on=["model", "input"]` for embeddings.
- **You don't care about a usually-load-bearing field** (e.g. you want one
  recording to serve any model in a family): explicitly *omit* `model` from
  `match_on`. Rare, but valid.

Rule of thumb: if changing field X *should* produce a different response in
your test, X belongs in `match_on`. If it shouldn't, leave it out.

### Switching providers without changing test code

```python
@pytest.fixture(params=["openai", "openrouter", "self_hosted_vllm"])
def llm(request):
    base = {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "self_hosted_vllm": "http://localhost:8000/v1",
    }[request.param]
    return OpenAI(base_url=base, api_key=os.environ.get(f"{request.param.upper()}_KEY", "x"))

def test_summarize(llm, tapelog_cassette):
    # one cassette per provider, auto-named to include the param
    ...
```

pytest's parametrization includes `[openai]` / `[openrouter]` / etc. in the
test node name, so the fixture's auto-named cassette becomes
`test_summarize[openai].yaml` etc. — one recording per provider, all from one
test function.

### Reasoning models

Reasoning models work without special handling. Two specifics worth knowing:

**1. The reasoning knobs are in the default `match_on`,** so this Just Works:
```python
@tapelog.use_cassette("cassettes/o3_high.yaml")
def test_o3_high():
    r = OpenAI().chat.completions.create(
        model="o3-mini", reasoning_effort="high",
        messages=[{"role": "user", "content": "solve: 17 * 23"}])
    ...

@tapelog.use_cassette("cassettes/o3_low.yaml")
def test_o3_low():
    r = OpenAI().chat.completions.create(
        model="o3-mini", reasoning_effort="low",
        messages=[{"role": "user", "content": "solve: 17 * 23"}])
    ...
```
The two recordings are kept separate because the fingerprint differs — no
silent cross-test pollution.

**2. Reasoning content is recorded.** Claude `thinking` blocks, o-series
`reasoning_content`, encrypted reasoning blobs (`reasoning.encrypted_content`)
all round-trip in the response body. If your assertion looks at the reasoning
trace, that works.

**3. Streamed reasoning deltas also work.** Same SSE machinery; the reasoning
events get captured and re-emitted in order.

### Multimodal: images, audio, vision

**Vision / image input.** The image is in the request body (typically base64 in
a content block). It's just JSON — tapelog records the request normally and
the prompt-with-image becomes part of the fingerprint.

**Image output / Claude image / GPT-4o image.** The image is base64 inside a
JSON content block in the response. tapelog records the JSON; the base64
round-trips byte-exact.

**Raw binary responses** (`Content-Type: image/png`, `audio/wav`, etc.). Detected
automatically, base64-encoded in the cassette with a `binary: true` flag,
decoded back to bytes on replay. Verified byte-equal in the test suite.

What doesn't work:

- **`multipart/form-data` requests** (audio transcription uploads, file
  attachments). Out of scope for v1.

---

## Part 4: Anti-patterns

Things that *look* like they should work but lead to pain:

**Don't put secrets in the prompt body and rely on tapelog to redact them.**
Only headers are auto-redacted. If you record a prompt that contains real PII
or a real API key, that data ends up in the committed YAML. Use synthetic data,
or scrub before commit (see [Compliance and PII](#compliance-and-pii)).

**Don't fuzzy-match in CI.** A future opt-in `fuzzy=True` flag will allow
semantic-similarity matching during local dev — never use it in CI. It
reintroduces the exact non-determinism you adopted tapelog to remove.

**Don't share one cassette across many unrelated tests.** Use the
`tapelog_cassette` fixture or one file per test. Sharing a cassette across
tests works mechanically (the fingerprints find the right interaction) but
makes the cassette diff in PR review unreadable.

**Don't intercept and mock LLM responses *in addition to* tapelog.** You'll
either double-mock (tapelog records the mock, not the real response) or
fight over the httpx transport. Pick one. tapelog is for the cases where you
want a real recording; conventional mocking is for everything else.

**Don't commit cassettes recorded against an unstable upstream.** If you're
recording against your own dev server that returns different shapes hour to
hour, those cassettes become bug reports against a future-you. Either freeze
the dev server's behavior first, or record against the prod-equivalent.

**Don't put a cassette inside a Docker image and rebuild every time.**
Cassettes live next to tests in the source tree. Re-recording is a git
operation, not a build operation.

---

## Where to go from here

- [README](README.md) — the elevator pitch and quickstart
- [SUPPORT.md](SUPPORT.md) — what's covered, what isn't, with workarounds
- [DESIGN.md](DESIGN.md) — *why* tapelog makes the choices it does
- `tests/` — the working examples of every claim in this doc

If a scenario you actually live in isn't covered here, file an issue with the
shape of your real workflow. The tutorials that matter come from real use, not
the author's guesses.
