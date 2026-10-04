"""Locatable error for the independent document engine, independent of host HTTP or task status."""

from __future__ import annotations


class DocumentError(ValueError):
    """Document input or processing error with stable error code."""

    def __init__(self, code: str, message: str, param: str | None = None) -> None:
        """Save error information for command line and upper-layer application mapping."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param


class InvalidRequestError(DocumentError):
    """Indicates that the page range or formatting options do not comply with the document input contract."""


__all__ = ["DocumentError", "InvalidRequestError"]
