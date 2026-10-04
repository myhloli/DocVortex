"""Passing an exclusive copy in an internal render call does not cache the user's mutable document."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from ....schema import MiddleJson


@dataclass(slots=True)
class _OwnedRenderDocument:
    """Only one planner is allowed to consume documents that the current call has isolated."""

    document: MiddleJson
    consumed: bool = False


_owned_document: ContextVar[_OwnedRenderDocument | None] = ContextVar("docvortex_owned_render_document", default=None)


@contextmanager
def owned_render_document(document: MiddleJson) -> Iterator[None]:
    """Limit the calling scope of the exclusive document, and restore the outer context after exceptions and reentrants."""
    token = _owned_document.set(_OwnedRenderDocument(document))
    try:
        yield
    finally:
        _owned_document.reset(token)


def claim_owned_document(document: MiddleJson) -> bool:
    """The exact object of the current call is only reused once, callback reentrants must be re-isolated."""
    context = _owned_document.get()
    if context is None or context.document is not document or context.consumed:
        return False
    context.consumed = True
    return True


__all__ = ["owned_render_document", "claim_owned_document"]
