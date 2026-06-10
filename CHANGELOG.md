# Changelog

All notable changes to promptecho. Pre-1.0: minor versions may break — each
breaking change is called out explicitly.

## Unreleased (0.1.3)

### Fixed
- **`use_cassette` as a decorator on `async def` functions was a silent no-op**:
  the httpx patch was removed before the coroutine ran, so nothing was recorded
  and every run made a live API call — even in `mode="none"`. Async functions
  now get an async wrapper that holds the patch open across the await.
- The field-level diff in a `CassetteMiss` error is now computed against the
  *most similar* recording in the cassette, not simply the last one.

### Changed — **cassette format v2 (breaking)**
- Match keys now include the **HTTP method and URL path**, so two endpoints
  called with the same body can never replay each other's recordings. The host
  is deliberately excluded (a recording made against one gateway replays
  through another).
- Requests whose body is not a JSON object (multipart, form-encoded, raw
  binary) are keyed by a **sha256 of their exact bytes** instead of all
  colliding on an empty parse.
- Version-1 cassettes are refused at load with an instruction to re-record
  (their keys can never match v2 keys). Re-record a whole suite with
  `PROMPTECHO_MODE=all pytest`.

### Added
- `PROMPTECHO_MODE` environment variable overrides the record mode for every
  cassette in the process (`PROMPTECHO_MODE=all pytest` refreshes all fixtures).
- `@pytest.mark.promptecho(mode=..., match_on=..., on_record_error=...)`
  configures the `promptecho_cassette` fixture per test.
- URL **query-string values are redacted** in cassettes (query-param auth such
  as `?key=…` no longer reaches disk); `set-cookie` added to redacted headers.
- Nested or concurrent `use_cassette` blocks now raise `RuntimeError`
  immediately instead of silently interleaving recordings (promptecho patches
  httpx process-wide, one cassette at a time; pytest-xdist is unaffected).
- `py.typed` marker — type hints are now visible to mypy/pyright.
- Fixed `CI=false` / `CI=0` being treated as CI by the pytest fixture's
  mode default.

## 0.1.2 — 2026-06-09

### Added
- `on_record_error` policy (`"warn"` default / `"raise"` / `"record"`): a
  transient 401/429/5xx during recording is no longer silently baked into the
  cassette. `warn` emits `PromptechoRecordingWarning` (convertible to a hard
  error via `warnings.filterwarnings`), `raise` aborts before the poisoned
  cassette reaches disk.

## 0.1.1 — 2026-06-09

### Changed (breaking)
- `CassetteMiss` (and later `RecordedErrorResponse`) inherit from
  `BaseException` instead of `Exception`, so LLM SDKs' `except Exception:`
  transport wrappers can't swallow the field-level diff. If you caught it
  with `except Exception:`, catch `CassetteMiss` explicitly instead.

### Added
- Field-level diff on cassette miss: the error names the exact changed path
  (`messages[1].content: recorded … / incoming …`) instead of "no match".

## 0.1.0 — 2026-06-06

Initial release: httpx sync + async transport interception, record modes
(`once` / `none` / `new_episodes` / `all`), SSE streaming record/replay,
binary response round-trip, per-provider request normalizers
(Anthropic / OpenAI / generic), reasoning-model match defaults, pytest plugin
with auto-named cassettes, header redaction.
