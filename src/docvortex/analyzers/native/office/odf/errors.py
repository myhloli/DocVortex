"""OpenDocument Internal stability error type."""

from __future__ import annotations


class OdfParseError(ValueError):
    """Indicates OpenDocument The package or semantic structure cannot be parsed."""


class OdfResourceLimitError(OdfParseError):
    """Indicates that the OpenDocument input exceeds the fixed safety margin."""


class OdfEncryptedError(OdfParseError):
    """Indicates that the OpenDocument package contains unsupported cryptographic members."""


__all__ = ["OdfEncryptedError", "OdfParseError", "OdfResourceLimitError"]
