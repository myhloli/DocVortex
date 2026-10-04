"""Collect structured diagnostics exported by PDF once, while retaining the underlying logs."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from loguru import logger

from ....result import Diagnostic


_diagnostics: ContextVar[list[Diagnostic] | None] = ContextVar("pdf_diagnostics", default=None)


@contextmanager
def collect_pdf_diagnostics() -> Iterator[list[Diagnostic]]:
    """Isolate diagnostics for concurrent or reentrant calls and restore the outer collector on exit."""
    items: list[Diagnostic] = []
    token = _diagnostics.set(items)
    try:
        yield items
    finally:
        _diagnostics.reset(token)


def report_pdf_diagnostic(code: str, message: str, page_index: int | None = None) -> None:
    """PDF rendering diagnostics use DEBUG logs uniformly, and structured diagnostics remain intact."""
    logger.debug("{}: {}", code, message)
    items = _diagnostics.get()
    if items is not None:
        diagnostic = Diagnostic(code, message, page_index)
        if diagnostic not in items:
            items.append(diagnostic)
