"""promptecho — record & replay for LLM API calls.

Public API:
    promptecho.use_cassette(path, mode="once", match_on=None,
                            on_record_error="warn")             # decorator + context manager
    promptecho.Mode                                              # record modes
    promptecho.CassetteMiss                                      # raised in mode='none' on miss
    promptecho.RecordedErrorResponse                             # raised in on_record_error='raise'
    promptecho.PromptechoRecordingWarning                        # emitted in on_record_error='warn'
"""

from __future__ import annotations

import functools
import inspect
import os
from contextlib import contextmanager

from .cassette import Cassette, PromptechoRecordingWarning
from .transport import CassetteMiss, Mode, RecordedErrorResponse

__all__ = [
    "use_cassette", "Mode", "Cassette",
    "CassetteMiss", "RecordedErrorResponse", "PromptechoRecordingWarning",
]
__version__ = "0.1.2"

_VALID_ON_RECORD_ERROR = ("record", "warn", "raise")


@contextmanager
def _activate(cassette: Cassette, mode: Mode, on_record_error: str = "warn"):
    """Patch httpx for the duration of the block, then restore and flush.

    While active, every httpx-based client (Anthropic, OpenAI, raw httpx) routes
    through the record/replay decision (see patch.py / DESIGN.md §1).
    """
    from .patch import install, uninstall

    if mode is Mode.ALL:
        cassette.interactions.clear()  # re-record from scratch
        cassette._dirty = True

    saved = install(cassette, mode, on_record_error)
    try:
        yield cassette
    finally:
        uninstall(saved)
        cassette.save()


class _UseCassette:
    """Works as both a decorator and a context manager (like vcrpy.use_cassette)."""

    def __init__(
        self,
        path: str,
        mode: str | Mode = Mode.ONCE,
        match_on=None,
        on_record_error: str = "warn",
    ):
        if on_record_error not in _VALID_ON_RECORD_ERROR:
            raise ValueError(
                f"on_record_error must be one of {_VALID_ON_RECORD_ERROR}, "
                f"got {on_record_error!r}"
            )
        self.path = path
        # PROMPTECHO_MODE overrides every cassette's mode — the suite-wide
        # workflow for "my prompts changed, refresh all fixtures":
        #     PROMPTECHO_MODE=all pytest
        env_mode = os.environ.get("PROMPTECHO_MODE", "").strip()
        self.mode = Mode(env_mode) if env_mode else Mode(mode)
        self.match_on = match_on
        self.on_record_error = on_record_error

    def _load(self) -> Cassette:
        return Cassette.load(self.path, match_on=self.match_on)

    def __enter__(self):
        self._cm = _activate(self._load(), self.mode, self.on_record_error)
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def __call__(self, func):
        # An async function returns its coroutine immediately; a sync wrapper
        # would exit (and unpatch httpx) before the coroutine ever runs, so the
        # patch must be held open across the await.
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with _activate(self._load(), self.mode, self.on_record_error):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _activate(self._load(), self.mode, self.on_record_error):
                return func(*args, **kwargs)

        return wrapper


def use_cassette(
    path: str,
    mode: str | Mode = Mode.ONCE,
    match_on=None,
    on_record_error: str = "warn",
) -> _UseCassette:
    """Record on first run, replay forever after.

    Usage as a decorator::

        @promptecho.use_cassette("cassettes/foo.yaml")
        def test_foo(): ...

    or as a context manager::

        with promptecho.use_cassette("cassettes/foo.yaml", mode="none"):
            client.messages.create(...)

    The ``PROMPTECHO_MODE`` environment variable, when set, overrides ``mode``
    for every cassette in the process — e.g. ``PROMPTECHO_MODE=all pytest``
    re-records an entire suite after a prompt change.

    ``on_record_error`` controls what happens when the upstream returns a
    non-2xx response that would otherwise be silently baked into the cassette
    (an expired API key, a rate limit, a transient 5xx):

    - ``"warn"`` *(default)* — emit :class:`PromptechoRecordingWarning` and
      record. Surfaces the footgun without breaking legitimate uses; convert
      to a hard CI error project-wide via ``warnings.filterwarnings("error",
      category=PromptechoRecordingWarning)``.
    - ``"raise"`` — raise :class:`RecordedErrorResponse` *before* writing the
      cassette, so no poisoned fixture reaches disk. Right for strict
      record-only CI pipelines.
    - ``"record"`` — silent capture; the v0.1.x behavior. Use when the test
      legitimately wants to record an error response (verifying a retry or
      error-handling path).
    """
    return _UseCassette(path, mode=mode, match_on=match_on, on_record_error=on_record_error)
