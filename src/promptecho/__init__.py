"""promptecho — record & replay for LLM API calls.

Public API:
    promptecho.use_cassette(path, mode="once", match_on=None)   # decorator + context manager
    promptecho.Mode                                             # record modes
    promptecho.CassetteMiss                                     # exception raised in mode='none'
"""

from __future__ import annotations

import functools
from contextlib import contextmanager

from .cassette import Cassette
from .transport import CassetteMiss, Mode

__all__ = ["use_cassette", "Mode", "Cassette", "CassetteMiss"]
__version__ = "0.1.1"


@contextmanager
def _activate(cassette: Cassette, mode: Mode):
    """Patch httpx for the duration of the block, then restore and flush.

    While active, every httpx-based client (Anthropic, OpenAI, raw httpx) routes
    through the record/replay decision (see patch.py / DESIGN.md §1).
    """
    from .patch import install, uninstall

    if mode is Mode.ALL:
        cassette.interactions.clear()  # re-record from scratch
        cassette._dirty = True

    saved = install(cassette, mode)
    try:
        yield cassette
    finally:
        uninstall(saved)
        cassette.save()


class _UseCassette:
    """Works as both a decorator and a context manager (like vcrpy.use_cassette)."""

    def __init__(self, path: str, mode: str | Mode = Mode.ONCE, match_on=None):
        self.path = path
        self.mode = Mode(mode)
        self.match_on = match_on

    def _load(self) -> Cassette:
        return Cassette.load(self.path, match_on=self.match_on)

    def __enter__(self):
        self._cm = _activate(self._load(), self.mode)
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _activate(self._load(), self.mode):
                return func(*args, **kwargs)

        return wrapper


def use_cassette(path: str, mode: str | Mode = Mode.ONCE, match_on=None) -> _UseCassette:
    """Record on first run, replay forever after.

    Usage as a decorator::

        @promptecho.use_cassette("cassettes/foo.yaml")
        def test_foo(): ...

    or as a context manager::

        with promptecho.use_cassette("cassettes/foo.yaml", mode="none"):
            client.messages.create(...)
    """
    return _UseCassette(path, mode=mode, match_on=match_on)
