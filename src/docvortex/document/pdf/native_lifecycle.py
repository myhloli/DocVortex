"""PDF The native sub-resource is closed and the native extraction algorithm and resource semantics are maintained."""

from __future__ import annotations

import logging

logger = logging.getLogger("docvortex.document.pdf._document")


def _try_close(obj: object) -> None:
    """Try your best to close the original sub-resource, and cleanup failure must not overwrite the original parsing exception."""
    if callable(close := getattr(obj, "close", None)):
        try:
            close()
        except Exception:
            pass
