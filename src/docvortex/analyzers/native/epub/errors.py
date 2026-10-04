"""EPUB Stable error type used internally by the parser."""


class EpubError(ValueError):
    """Common base class for all EPUB parsing errors."""


class EpubParseError(EpubError):
    """Indicates EPUB The container or body structure is unavailable."""


class EpubEncryptedError(EpubError):
    """Indicates that the EPUB resource required for parsing has been encrypted."""


class EpubResourceLimitError(EpubError):
    """Indicates that the EPUB input exceeds the fixed resource budget."""


__all__ = ["EpubEncryptedError", "EpubError", "EpubParseError", "EpubResourceLimitError"]
